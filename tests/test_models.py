import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from dcp import tokens
from dcp.cli import main
from dcp.config import load_rules
from dcp.events import EventStore
from dcp.flowstate import next_orders
from dcp.models import (ACTION_KIND, DEFAULT_MODELS, assign, effective_models, escalate, tier_alias, validate_models)
from dcp.project import load_project, make_project, validate_project, with_defaults
from dcp.scoring import evaluate_store
from dcp.tick import launch, tick

DM, TL = "U0EXAMPLE1", "U0EXAMPLE2"
ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
RULES = load_rules()
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            code = main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if not isinstance(e.code, int):
                buf.write(str(e.code))
    return code, buf.getvalue()


def build_cfg(**kw):
    return with_defaults(make_project("SP", "build", "https://pulse.iksula.com/projects/SP/board", "#sp", "S",
                                      "iksula/x", delivery_manager="M:U0EXAMPLE1", tech_lead="A:U0EXAMPLE2", **kw))


class ModelConfigTests(unittest.TestCase):
    def test_every_step_has_a_kind_and_a_default(self):
        self.assertEqual(set(DEFAULT_MODELS), set(ACTION_KIND))
        self.assertEqual({a for a, k in ACTION_KIND.items() if k == "code"},
                         {"lint_pack", "lint_tickets", "check_drift", "assess_call"})
        for a, k in ACTION_KIND.items():
            self.assertEqual(DEFAULT_MODELS[a] == "none", k == "code", a)

    def test_new_projects_carry_an_explicit_models_block_that_validates(self):
        cfg = make_project("SP", "build", "https://pulse.iksula.com/projects/SP/board", "#sp", "S", "iksula/x",
                           delivery_manager="M:U0EXAMPLE1", tech_lead="A:U0EXAMPLE2")
        self.assertEqual(cfg["models"], DEFAULT_MODELS)
        self.assertEqual(validate_project(cfg), [])

    def test_old_project_files_without_a_models_block_still_work(self):
        cfg = {"key": "SP", "track": "build"}
        self.assertEqual(with_defaults(cfg)["models"], DEFAULT_MODELS)
        self.assertEqual(with_defaults(cfg)["runner"]["mode"], "claude-session")

    def test_invalid_settings_are_refused_with_the_reason(self):
        cfg = build_cfg()
        cfg["models"].update({"draft_pack": "none", "lint_pack": "strong", "poll_slack": "none", "bogus": "small", "nudge": "huge"})
        text = " | ".join(validate_models(cfg))
        for needle in ("drafting needs a model", "plain code", "needs direct API access", "unknown step", "not a tier"):
            self.assertIn(needle, text)

    def test_connector_steps_can_have_no_model_only_with_api_access(self):
        cfg = build_cfg()
        cfg["models"]["poll_slack"] = "none"
        self.assertTrue(validate_models(cfg))
        cfg["runner"]["mode"] = "api"
        self.assertEqual(validate_models(cfg), [])
        o = type("O", (), {"action": "poll_slack", "inputs": {}})()
        assign(o, [], cfg, RULES)
        self.assertEqual((o.tier, o.model, o.needs_ai), ("none", None, False))

    def test_tier_aliases_come_from_the_rules_file(self):
        self.assertEqual(tier_alias("standard", RULES), "sonnet")
        self.assertIsNone(tier_alias("none", RULES))
        changed = json.loads(json.dumps(RULES))
        changed["models"]["tiers"]["standard"] = "some-future-model"
        self.assertEqual(tier_alias("standard", changed), "some-future-model")

    def test_escalation_steps_up_after_repeated_failures_and_stops_at_strong(self):
        self.assertEqual(escalate("standard", 0, 2), ("standard", False))
        self.assertEqual(escalate("standard", 1, 2), ("standard", False))
        self.assertEqual(escalate("standard", 2, 2), ("strong", True))
        self.assertEqual(escalate("small", 2, 2), ("standard", True))
        self.assertEqual(escalate("small", 4, 2), ("strong", True))
        self.assertEqual(escalate("strong", 9, 2), ("strong", False))
        self.assertEqual(escalate("none", 9, 2), ("none", False))


class OrderModelTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.dir.name) / "e.jsonl")
        self.cfg = build_cfg()

    def tearDown(self):
        self.dir.cleanup()

    def add(self, type_, data, minutes=0, flow="SP-REQ-001"):
        self.store.append(type_, flow, "SP", "a", data, ts=T0 + timedelta(minutes=minutes))

    def orders(self, minutes=500):
        o, _ = next_orders(self.store, self.cfg, T0 + timedelta(minutes=minutes), RULES)
        return {x.action: x for x in o if x.flow != "_poll"}

    def ready(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("call.assessed", {"covered": ["a"] * 6, "missing": [], "ready": True, "needed": 5}, 0)

    def test_drafting_runs_on_standard_and_connectors_on_small(self):
        self.ready()
        o = self.orders()["draft_pack"]
        self.assertEqual((o.tier, o.model, o.needs_ai, o.step_kind), ("standard", "sonnet", True, "ai"))
        self.cfg["granola"]["source"] = "connector"
        polls, _ = next_orders(self.store, self.cfg, T0, RULES)
        g = next(p for p in polls if p.action == "poll_granola")
        self.assertEqual((g.tier, g.model, g.needs_ai), ("small", "haiku", False))

    def test_code_steps_never_get_a_model(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("pack.drafted", {"version": 1}, 1)
        o = self.orders()["lint_pack"]
        self.assertEqual((o.tier, o.model, o.needs_ai, o.step_kind), ("none", None, False, "code"))

    def test_a_failing_pack_escalates_the_redraft_to_a_stronger_model(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("pack.drafted", {"version": 1}, 1)
        self.add("pack.linted", {"version": 1, "passed": False, "blocks": 3, "warns": 0}, 2)
        first = self.orders()["fix_pack"]
        self.assertEqual((first.tier, "escalated_to" in first.inputs), ("standard", False))  # one failure: same tier
        self.add("pack.drafted", {"version": 2}, 3)
        self.add("pack.linted", {"version": 2, "passed": False, "blocks": 2, "warns": 0}, 4)
        second = self.orders()["fix_pack"]
        self.assertEqual((second.tier, second.model, second.inputs["escalated_to"]), ("strong", "opus", "strong"))

    def test_the_escalation_limit_is_configurable(self):
        self.cfg["escalate_after_failures"] = 1
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("pack.drafted", {"version": 1}, 1)
        self.add("pack.linted", {"version": 1, "passed": False, "blocks": 3, "warns": 0}, 2)
        self.assertEqual(self.orders()["fix_pack"].tier, "strong")

    def test_projects_can_choose_a_different_tier_per_step(self):
        self.cfg["models"]["draft_pack"] = "strong"
        self.cfg["models"]["poll_granola"] = "standard"
        self.cfg["granola"]["source"] = "connector"
        self.ready()
        self.assertEqual(self.orders()["draft_pack"].model, "opus")
        polls, _ = next_orders(self.store, self.cfg, T0, RULES)
        self.assertEqual(next(p for p in polls if p.action == "poll_granola").model, "sonnet")

    def test_next_json_says_whether_any_ai_is_needed(self):
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        base = ["--projects-dir", str(d / "projects"), "--data-dir", str(d / "data")]
        data = json.loads(run("next", "--project", "SP", *base, "--json")[1])
        self.assertFalse(data["needs_ai"])  # only polls are due on a new project
        run("flow-new", "--project", "SP", *base, "--source-ref", "g", "--title", "S :: t :: SP")
        data = json.loads(run("next", "--project", "SP", *base, "--json")[1])
        self.assertFalse(data["needs_ai"])  # the call is assessed by code first
        self.assertIn("assess_call", [o["action"] for o in data["orders"]])
        run("assess-call", "--project", "SP", *base, "--flow", "SP-REQ-001", "--transcript", str(EX / "transcript.txt"))
        data = json.loads(run("next", "--project", "SP", *base, "--json")[1])
        self.assertTrue(data["needs_ai"])
        self.assertTrue(all("model" in o and "tier" in o for o in data["orders"]))


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.dir.name) / "e.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def ai(self, action, model, tin, tout, minutes, flow="F1", tier=None):
        data = {"action": action, "model": model, "input_tokens": tin, "output_tokens": tout}
        if tier:
            data["tier"] = tier
        self.store.append("ai.used", flow, "SP", "runner", data, ts=T0 + timedelta(minutes=minutes))

    def lint(self, passed, minutes, flow="F1"):
        self.store.append("pack.linted", flow, "SP", "dcp", {"version": 1, "passed": passed, "blocks": 0 if passed else 3, "warns": 0},
                          ts=T0 + timedelta(minutes=minutes))

    def test_totals_by_step_and_model_with_relative_cost(self):
        self.ai("draft_pack", "sonnet", 9000, 2000, 1)
        self.ai("prepare_questions", "haiku", 3000, 500, 2)
        s = tokens.summarize(self.store.events(), RULES)
        self.assertEqual((s["total"]["calls"], s["total"]["tokens"]), (2, 14500))
        self.assertEqual(s["by_action"]["draft_pack"]["avg_tokens_per_call"], 11000)
        # (11000 * 3 + 3500 * 1) / 1000 with the placeholder weights 3 and 1
        self.assertEqual(s["total"]["cost_units"], 36.5)
        self.assertEqual(s["by_model"]["haiku"]["calls"], 1)

    def test_a_model_with_no_price_is_counted_but_not_priced(self):
        self.ai("draft_pack", "mystery-model", 1000, 1000, 1)
        s = tokens.summarize(self.store.events(), RULES)
        self.assertEqual((s["total"]["tokens"], s["total"]["cost_units"], s["total"]["unpriced_tokens"]), (2000, 0.0, 2000))

    def test_tier_on_the_event_prices_a_model_alias_the_rules_do_not_know(self):
        self.ai("draft_pack", "some-new-model", 1000, 1000, 1, tier="strong")
        self.assertEqual(tokens.summarize(self.store.events(), RULES)["total"]["cost_units"], 30.0)

    def test_draft_outcomes_by_model_for_the_bake_off(self):
        # sonnet: first draft fails, redraft passes. haiku: first draft passes in another flow.
        self.ai("draft_pack", "sonnet", 9000, 2000, 1)
        self.lint(False, 2)
        self.ai("fix_pack", "sonnet", 9000, 2000, 3)
        self.lint(True, 4)
        self.ai("draft_pack", "haiku", 9000, 2000, 1, flow="F2")
        self.lint(True, 2, flow="F2")
        oc = tokens.outcomes(self.store.events())
        self.assertEqual((oc["sonnet"]["drafts"], oc["sonnet"]["passed"], oc["sonnet"]["first_try_pass_rate"]), (2, 1, 0))
        self.assertEqual((oc["haiku"]["drafts"], oc["haiku"]["first_try_pass_rate"]), (1, 100))

    def test_a_draft_with_no_verdict_yet_is_not_counted(self):
        self.ai("draft_pack", "sonnet", 100, 100, 1)
        self.assertEqual(tokens.outcomes(self.store.events()), {})

    def test_flow_metrics_carry_total_tokens_for_the_governance_report(self):
        self.store.append("call.recorded", "F1", "SP", "a", {"title": "x :: y :: SP"}, ts=T0)
        self.assertIsNone(evaluate_store(self.store, RULES, flow="F1")[0].metrics["tokens_total"])
        self.ai("draft_pack", "sonnet", 100, 50, 1)
        m = evaluate_store(self.store, RULES, flow="F1")[0].metrics
        self.assertEqual((m["tokens_total"], m["ai_calls"]), (150, 1))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.base = ["--projects-dir", str(d / "projects"), "--data-dir", str(d / "data")]
        self.store_path = d / "data" / "SP" / "events.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def ai_used(self, action, model="sonnet", tin=100, tout=50):
        return run("ai-used", "--project", "SP", *self.base, "--flow", "SP-REQ-001", "--action", action,
                   "--model", model, "--input-tokens", str(tin), "--output-tokens", str(tout))

    def test_models_command_shows_and_saves(self):
        code, out = run("models", "SP", "--projects-dir", str(self.d / "projects"))
        self.assertEqual(code, 0)
        self.assertIn("draft_pack", out)
        code, out = run("models", "SP", "--projects-dir", str(self.d / "projects"), "--set", "draft_pack=strong", "--escalate-after", "3")
        self.assertEqual(code, 0, out)
        saved = json.loads((self.d / "projects" / "SP.json").read_text())
        self.assertEqual((saved["models"]["draft_pack"], saved["escalate_after_failures"]), ("strong", 3))

    def test_models_command_refuses_and_does_not_save_a_bad_change(self):
        before = (self.d / "projects" / "SP.json").read_text()
        code, out = run("models", "SP", "--projects-dir", str(self.d / "projects"), "--set", "lint_pack=strong")
        self.assertEqual(code, 1)
        self.assertIn("not saved", out)
        self.assertEqual((self.d / "projects" / "SP.json").read_text(), before)
        self.assertEqual(run("models", "SP", "--projects-dir", str(self.d / "projects"), "--set", "oops")[0], 1)

    def test_ai_used_logs_and_refuses_code_steps_unknown_steps_and_negative_counts(self):
        self.assertEqual(self.ai_used("draft_pack")[0], 0)
        self.assertEqual(self.ai_used("lint_pack")[0], 1)        # code never uses a model
        self.assertEqual(self.ai_used("made_up_step")[0], 1)
        self.assertEqual(self.ai_used("draft_pack", tin=-5)[0], 1)
        evs = [e for e in EventStore(self.store_path).events() if e.type == "ai.used"]
        self.assertEqual(len(evs), 1)

    def test_tokens_command_reports_and_says_so_when_empty(self):
        self.assertIn("no AI calls logged", run("tokens", "--project", "SP", *self.base)[1])
        self.ai_used("draft_pack", tin=9000, tout=2000)
        code, out = run("tokens", "--project", "SP", *self.base)
        self.assertEqual(code, 0)
        self.assertIn("11,000 tokens", out)
        self.assertIn("placeholder weights", out)
        data = json.loads(run("tokens", "--project", "SP", *self.base, "--json")[1])
        self.assertEqual(data["summary"]["total"]["tokens"], 11000)


class CallReadinessTests(unittest.TestCase):
    """A call that does not cover what a requirement needs is a follow-up call. Catch it before spending tokens."""

    # long enough to be a call (over 100 words) but it never covers what a requirement needs
    THIN = ("Me: We talked about the website layout and how the offer is described, so the page should not feel crowded. "
            "The photos need to be resized into smaller versions and the menu needs a simpler structure. "
            + "We went through the site map and how the sections are ordered for visitors and partners. " * 6
            + "Start sending the newsletter to subscribers.")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.base = ["--projects-dir", str(d / "projects"), "--data-dir", str(d / "data")]
        self.fd = d / "data" / "SP" / "SP-REQ-001"
        self.fd.mkdir(parents=True)
        for st in ("granola", "pulse_sweep", "governance"):
            EventStore(d / "data" / "SP" / "events.jsonl").append("poll.ran", "_poll", "SP", "r", {"stage": st})
        run("flow-new", "--project", "SP", *self.base, "--source-ref", "g", "--title", "S :: t :: SP")

    def tearDown(self):
        self.dir.cleanup()

    def nxt(self):
        return json.loads(run("next", "--project", "SP", *self.base, "--json")[1])

    def actions(self):
        return [o["action"] for o in self.nxt()["orders"] if o["flow"] == "SP-REQ-001"]

    def assess(self, text):
        (self.fd / "transcript.txt").write_text(text)
        return run("assess-call", "--project", "SP", *self.base, "--flow", "SP-REQ-001")

    def test_the_tightened_keywords_do_not_count_generic_phrases(self):
        from dcp.packlint import assess_call
        r = assess_call(self.THIN, RULES)
        self.assertEqual((r["covered"], r["ready"]), ([], False))  # 'need to' and 'should not' no longer count

    def test_a_paste_that_is_too_short_is_not_a_call_at_all(self):
        code, out = self.assess("Me: short note about a thing we need to do soon.")
        self.assertIn("too short", out)
        w = [x for x in self.nxt()["waiting"] if x["waiting_for"] == "followup_call"]
        self.assertIn("not usable", w[0]["detail"])
        self.assertNotIn("draft_pack", self.actions())

    def test_an_ai_summary_is_refused_even_if_placed_straight_into_the_flow_folder(self):
        summary = "# Notes\n" + "\n".join(f"- point number {i} about the discussion and what was agreed with the client team" for i in range(14)) + "\n# Next Steps\n- do the thing\n- do another thing\n"
        code, out = self.assess(summary)
        self.assertIn("AI summary", out)
        self.assertNotIn("draft_pack", self.actions())
        self.assertIn("summary", [x for x in self.nxt()["waiting"] if x["waiting_for"] == "followup_call"][0]["detail"])

    def test_a_real_requirement_call_is_ready(self):
        from dcp.packlint import assess_call
        r = assess_call((EX / "transcript.txt").read_text(), RULES)
        self.assertEqual((len(r["covered"]), r["ready"]), (6, True))

    def test_a_thin_call_asks_for_a_follow_up_instead_of_drafting(self):
        code, out = self.assess(self.THIN)
        self.assertIn("NOT ready", out)
        acts = self.actions()
        self.assertIn("request_followup", acts)
        self.assertNotIn("draft_pack", acts)
        data = self.nxt()
        self.assertFalse(data["needs_ai"])  # nothing spent drafting
        w = [x for x in data["waiting"] if x["waiting_for"] == "followup_call"]
        self.assertEqual(w[0]["owner"], "delivery_manager")
        self.assertIn("what_done_looks_like", w[0]["detail"])

    def test_the_follow_up_request_is_posted_once_but_the_wait_stays_visible(self):
        self.assess(self.THIN)
        EventStore(self.d / "data" / "SP" / "events.jsonl").append("nudge.sent", "SP-REQ-001", "SP", "r", {"check": "followup"})
        self.assertNotIn("request_followup", self.actions())
        self.assertTrue([x for x in self.nxt()["waiting"] if x["waiting_for"] == "followup_call"])

    def test_only_the_delivery_manager_can_override_and_needs_a_reason(self):
        self.assess(self.THIN)
        args = ["call-proceed", "--project", "SP", *self.base, "--flow", "SP-REQ-001"]
        self.assertEqual(run(*args, "--reason", "client is travelling, scope is small", "--by-id", TL)[0], 1)
        self.assertEqual(run(*args, "--reason", "ok", "--by-id", DM)[0], 1)
        self.assertEqual(run(*args, "--reason", "x", "--by-id", "U0STRANGER")[0], 1)
        self.assertNotIn("draft_pack", self.actions())
        code, out = run(*args, "--reason", "client is travelling, scope is small", "--by-id", DM)
        self.assertEqual(code, 0, out)
        self.assertIn("draft_pack", self.actions())

    def test_a_ready_call_goes_straight_to_drafting(self):
        self.assess((EX / "transcript.txt").read_text())
        self.assertEqual(self.actions(), ["draft_pack"])

    def test_the_threshold_is_configurable(self):
        from dcp.packlint import assess_call
        loose = json.loads(json.dumps(RULES))
        loose["lint"]["call_ready_min_topics"] = 0
        self.assertTrue(assess_call(self.THIN, loose)["ready"])

    def test_the_tick_assesses_the_call_itself_with_no_tokens(self):
        (self.fd / "transcript.txt").write_text(self.THIN)
        cfg = load_project(self.d / "projects" / "SP.json")
        r = tick(cfg, RULES, self.d / "data", self.d / "data" / "SP" / "events.jsonl", self.d / "projects")
        self.assertIn("SP-REQ-001:assess_call", r["ran_without_ai"])
        orders = [o["action"] for s in r["sessions"] for o in s["orders"]]
        self.assertNotIn("draft_pack", orders)
        self.assertIn("request_followup", orders)

    def test_the_tick_reports_a_missing_transcript_instead_of_guessing(self):
        cfg = load_project(self.d / "projects" / "SP.json")
        r = tick(cfg, RULES, self.d / "data", self.d / "data" / "SP" / "events.jsonl", self.d / "projects")
        self.assertTrue(any("transcript.txt missing" in n for n in r["skipped"]))

    def test_assess_refuses_when_there_is_no_transcript(self):
        code, out = run("assess-call", "--project", "SP", *self.base, "--flow", "SP-REQ-001")
        self.assertEqual(code, 1)
        self.assertIn("no transcript", out)


class TickTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.cfg = load_project(d / "projects" / "SP.json")
        self.store_path = d / "data" / "SP" / "events.jsonl"
        self.store = EventStore(self.store_path)

    def tearDown(self):
        self.dir.cleanup()

    def polls_done(self):
        for st in ("granola", "pulse_sweep", "governance"):
            self.store.append("poll.ran", "_poll", "SP", "runner", {"stage": st})

    def go(self):
        return tick(self.cfg, RULES, self.d / "data", self.store_path, self.d / "projects")

    def test_an_idle_tick_needs_no_session_and_spends_nothing(self):
        self.polls_done()
        r = self.go()
        self.assertTrue(r["idle"])
        self.assertEqual((r["ran_without_ai"], r["sessions"], r["needs_ai_session"]), ([], [], False))

    def test_a_new_project_needs_a_small_session_for_its_polls(self):
        r = self.go()
        self.assertFalse(r["idle"])
        self.assertEqual([s["model"] for s in r["sessions"]], ["haiku"])

    def test_the_tick_runs_the_pack_check_itself_with_no_tokens(self):
        self.polls_done()
        self.store.append("call.recorded", "SP-REQ-001", "SP", "a", {"title": "S :: t :: SP"})
        self.store.append("pack.drafted", "SP-REQ-001", "SP", "a", {"version": 1})
        fd = self.d / "data" / "SP" / "SP-REQ-001"
        fd.mkdir(parents=True)
        shutil.copy(EX / "transcript.txt", fd / "transcript.txt")
        shutil.copy(EX / "pack_good.md", fd / "pack_v1.md")
        r = self.go()
        self.assertEqual(r["ran_without_ai"], ["SP-REQ-001:lint_pack"])
        lints = [e for e in self.store.events(flow="SP-REQ-001") if e.type == "pack.linted"]
        self.assertEqual((len(lints), lints[0].data["passed"]), (1, True))
        self.assertNotIn("lint_pack", [o["action"] for s in r["sessions"] for o in s["orders"]])

    def test_a_bad_pack_is_caught_and_the_redraft_is_routed_to_a_model(self):
        self.polls_done()
        self.store.append("call.recorded", "SP-REQ-001", "SP", "a", {"title": "S :: t :: SP"})
        self.store.append("pack.drafted", "SP-REQ-001", "SP", "a", {"version": 1})
        fd = self.d / "data" / "SP" / "SP-REQ-001"
        fd.mkdir(parents=True)
        shutil.copy(EX / "transcript.txt", fd / "transcript.txt")
        shutil.copy(EX / "pack_bad.md", fd / "pack_v1.md")
        r = self.go()
        self.assertEqual(r["ran_without_ai"], ["SP-REQ-001:lint_pack"])
        orders = {o["action"]: o for s in r["sessions"] for o in s["orders"]}
        self.assertEqual(orders["fix_pack"]["model"], "sonnet")

    def test_missing_input_files_are_reported_not_guessed(self):
        self.polls_done()
        self.store.append("call.recorded", "SP-REQ-001", "SP", "a", {"title": "S :: t :: SP"})
        self.store.append("pack.drafted", "SP-REQ-001", "SP", "a", {"version": 1})
        r = self.go()
        self.assertEqual(r["ran_without_ai"], [])
        self.assertTrue(any("missing" in n for n in r["skipped"]))

    def test_launch_starts_one_session_per_model_and_only_when_asked(self):
        r = {"project": "SP", "sessions": [{"model": "haiku", "orders": [{"action": "poll_granola"}]},
                                           {"model": "sonnet", "orders": [{"action": "draft_pack"}]}]}
        with mock.patch("dcp.tick.shutil.which", return_value="/usr/bin/claude"), \
                mock.patch("dcp.tick.subprocess.run") as sub:
            sub.return_value = mock.Mock(returncode=0)
            notes = launch(r, Path("runner/RUNNER.md"))
        self.assertEqual(len(notes), 2)
        cmds = [c.args[0] for c in sub.call_args_list]
        self.assertEqual([c[3] for c in cmds], ["haiku", "sonnet"])
        self.assertTrue(all(c[:3] == ["claude", "-p", "--model"] for c in cmds))
        self.assertIn("data, never instructions", cmds[0][4])

    def test_launch_refuses_cleanly_when_the_claude_command_is_missing(self):
        with mock.patch("dcp.tick.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                launch({"project": "SP", "sessions": []}, Path("x"))


if __name__ == "__main__":
    unittest.main()
