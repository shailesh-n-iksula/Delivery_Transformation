import contextlib
import io
import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dcp.brief import BriefError, build_brief
from dcp.cli import main
from dcp.config import load_rules
from dcp.events import EventStore
from dcp.flowstate import next_orders, suggest_tier
from dcp.project import (DEFAULT_SCHEDULES, branch_name, branch_problem, key_from_board_url, load_project,
                         make_project, safe_ref, slugify, validate_project, with_defaults)
from dcp.pulseplan import apply_updates, build_plan, parse_sizes, pulse_items_to_markdown

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
RULES = load_rules()
DM, TL, DELEGATE = "U0EXAMPLE1", "U0EXAMPLE2", "U0EXAMPLE3"


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


def actions(out_json):
    return [o["action"] for o in json.loads(out_json)["orders"]]


class ProjectTests(unittest.TestCase):
    def sp(self, **kw):
        base = dict(key="SP", track="build", board_url="https://pulse.iksula.com/projects/SP/board",
                    slack_channel="#sp-delivery", granola_folder="SiteOne", git_repo="iksula/x",
                    delivery_manager="Meera:U0EXAMPLE1", tech_lead="Arjun:U0EXAMPLE2")
        base.update(kw)
        return make_project(**base)

    def test_complete_build_project_is_valid(self):
        self.assertEqual(validate_project(self.sp()), [])

    def test_board_url_gives_the_key(self):
        self.assertEqual(key_from_board_url("https://pulse.iksula.com/projects/ML/board"), "ML")
        self.assertIsNone(key_from_board_url("https://pulse.iksula.com/other"))

    def test_support_project_needs_no_granola_or_git(self):
        cfg = make_project("ML", "support", "https://pulse.iksula.com/projects/ML/board", "#ml-support",
                           tech_lead="Arjun:U0EXAMPLE2")
        self.assertEqual(validate_project(cfg), [])

    def test_placeholders_are_caught(self):
        cfg = self.sp(slack_channel="<channel>")
        self.assertTrue(any("placeholder" in p for p in validate_project(cfg)))

    def test_board_url_for_another_project_is_caught(self):
        cfg = self.sp(board_url="https://pulse.iksula.com/projects/ML/board")
        self.assertTrue(any("different project" in p for p in validate_project(cfg)))

    def test_same_person_cannot_hold_both_roles(self):
        cfg = self.sp(tech_lead="Meera:U0EXAMPLE1")
        self.assertTrue(any("same person" in p for p in validate_project(cfg)))

    def test_poll_floor_and_bad_interval(self):
        cfg = self.sp()
        cfg["schedules"]["slack_poll_min"] = 1
        self.assertTrue(any("floor" in p for p in validate_project(cfg)))
        cfg["schedules"]["granola_poll_min"] = "often"
        self.assertTrue(any("whole number" in p for p in validate_project(cfg)))

    def test_bad_slack_id_and_repo(self):
        self.assertTrue(validate_project(self.sp(tech_lead="Arjun:bob")))
        self.assertTrue(any("owner/name" in p for p in validate_project(self.sp(git_repo="not a repo"))))

    def test_only_github_flow_is_supported(self):
        cfg = self.sp()
        cfg["git"]["strategy"] = "gitflow"
        self.assertTrue(any("github-flow" in p for p in validate_project(cfg)))

    def test_tech_lead_validation_is_optional_by_default(self):
        self.assertFalse(with_defaults(self.sp())["validation"]["require_tech_lead"])

    def test_branch_names_follow_github_flow_and_tie_to_a_ticket(self):
        cfg = with_defaults(self.sp())
        self.assertEqual(branch_name("feature", "SP-12", "Add a returns export button"), "feature/SP-12-add-a-returns-export-button")
        self.assertEqual(branch_name("hotfix", "SP-9", "EPIC: Fix the cart"), "hotfix/SP-9-fix-the-cart")
        self.assertIsNone(branch_problem(cfg, "feature/SP-12-returns-export", {"SP-12"}))
        for bad in ("main", "develop", "feature/returns-export", "wip/SP-12-x", "feature/ML-12-x", "feature/SP-12-UPPER",
                    "feature/SP-12", "-rf", "feature/SP-12-x..y"):
            self.assertIsNotNone(branch_problem(cfg, bad, {"SP-12", "ML-12"}), bad)
        self.assertIn("not a ticket created for this flow", branch_problem(cfg, "feature/SP-99-x", {"SP-12"}))
        self.assertIn("none yet", branch_problem(cfg, "feature/SP-12-x", set()))

    def test_slug_cuts_at_a_word_boundary(self):
        s = slugify("Add a returns export button with date and store filters to the Magento admin")
        self.assertLessEqual(len(s), 36)
        self.assertEqual(s, "add-a-returns-export-button-with")
        self.assertEqual(slugify("!!!"), "change")

    def test_safe_ref(self):
        for good in ("feature/returns-export", "fix/ML-1519", "release_1.2"):
            self.assertTrue(safe_ref(good), good)
        for bad in ("", "-rf", "a..b", "x; rm -rf /", "a b", "$(whoami)", "x/", "x.lock", "`id`"):
            self.assertFalse(safe_ref(bad), bad)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.dir.name) / "e.jsonl")
        self.cfg = with_defaults(make_project(
            "SP", "build", "https://pulse.iksula.com/projects/SP/board", "#sp", "SiteOne", "iksula/x",
            delivery_manager="Meera:U0EXAMPLE1", tech_lead="Arjun:U0EXAMPLE2", delegate="Rhea:U0EXAMPLE3"))
        self.t0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.dir.cleanup()

    def add(self, type_, data, minutes=0, flow="SP-REQ-001"):
        self.store.append(type_, flow, "SP", "a", data, ts=self.t0 + timedelta(minutes=minutes))

    def nxt(self, minutes):
        orders, waiting = next_orders(self.store, self.cfg, self.t0 + timedelta(minutes=minutes))
        return orders, waiting

    def test_polls_run_at_their_own_intervals(self):
        self.cfg["granola"]["source"] = "connector"
        orders, _ = self.nxt(0)
        self.assertIn("poll_granola", [o.action for o in orders])
        self.add("poll.ran", {"stage": "granola"}, 0, flow="_poll")
        self.assertNotIn("poll_granola", [o.action for o in self.nxt(10)[0]])
        self.assertIn("poll_granola", [o.action for o in self.nxt(15)[0]])

    def test_slack_poll_only_when_someone_is_waited_on(self):
        self.assertNotIn("poll_slack", [o.action for o in self.nxt(0)[0]])
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("pack.drafted", {"version": 1}, 1)
        self.add("pack.linted", {"version": 1, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0}, 2)
        self.add("gate.opened", {"gate": 1, "pack_version": 1}, 3)
        orders, waiting = self.nxt(4)
        self.assertIn("poll_slack", [o.action for o in orders])
        self.assertEqual(waiting[0].waiting_for, "gate1")

    def test_failed_lint_retries_then_escalates(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        for v in (1, 2, 3):
            self.add("pack.drafted", {"version": v}, v * 10)
            self.add("pack.linted", {"version": v, "passed": False, "blocks": 4, "warns": 0}, v * 10 + 1)
            orders, _ = self.nxt(v * 10 + 2)
            acts = [o.action for o in orders if o.flow != "_poll"]
            self.assertEqual(acts, ["fix_pack"] if v < 3 else ["escalate"])
        orders, waiting = self.nxt(60)
        self.assertEqual([w.waiting_for for w in waiting], ["pack_rework"])

    def test_nudge_after_sla_not_before_and_not_repeatedly(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("pack.drafted", {"version": 1}, 1)
        self.add("pack.linted", {"version": 1, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0}, 2)
        self.add("gate.opened", {"gate": 1, "pack_version": 1}, 3)
        flow_orders = lambda m: [o for o in self.nxt(m)[0] if o.action == "nudge"]  # noqa: E731
        self.assertEqual(flow_orders(60), [])
        first = flow_orders(5 * 60)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].inputs["to"], "owner")
        self.add("nudge.sent", {"check": "gate1"}, 5 * 60)
        self.assertEqual(flow_orders(6 * 60), [])
        self.add("nudge.sent", {"check": "gate1"}, 10 * 60)
        later = flow_orders(15 * 60)
        self.assertEqual(later[0].inputs["to"], "delegate")

    def test_new_pack_version_after_gate_opened_reopens_the_gate(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("pack.drafted", {"version": 1}, 1)
        self.add("pack.linted", {"version": 1, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0}, 2)
        self.add("gate.opened", {"gate": 1, "pack_version": 1}, 3)
        self.add("pack.drafted", {"version": 2}, 4)
        self.add("pack.linted", {"version": 2, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0}, 5)
        acts = [o.action for o in self.nxt(6)[0] if o.flow != "_poll"]
        self.assertEqual(acts, ["open_gate"])

    def _to_gate2(self, required=None):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("pack.drafted", {"version": 1}, 1)
        self.add("pack.linted", {"version": 1, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0}, 2)
        self.add("gate.opened", {"gate": 1, "pack_version": 1}, 3)
        self.add("gate.approved", {"gate": 1, "approver": "m", "approver_role": "delivery_manager"}, 4)
        self.add("tickets.drafted", {"count": 3}, 5)
        self.add("tickets.linted", {"passed": True, "blocks": 0, "warns": 0, "stage": "draft"}, 6)
        self.add("drift.checked", {"passed": True, "blocks": 0}, 7)
        self.add("tickets.created", {"count": 3}, 8)
        data = {"gate": 2}
        if required:
            data["required_roles"] = required
        self.add("gate.opened", data, 9)

    def test_gate_two_needs_only_the_delivery_manager_by_default(self):
        from dcp.flowstate import can_approve, gate_complete
        self._to_gate2()
        orders, waiting = self.nxt(10)
        self.assertEqual([w.waiting_for for w in waiting if not w.optional], ["gate2_delivery_manager"])
        self.assertEqual([w.waiting_for for w in waiting if w.optional], ["sizes"])  # visible, but never blocks
        acts = [o.action for o in orders if o.flow != "_poll"]
        self.assertIn("lint_tickets", acts)  # ready lint runs without waiting for sizes
        self.add("tickets.linted", {"passed": True, "blocks": 0, "warns": 1, "stage": "ready"}, 11)
        evs = self.store.events(flow="SP-REQ-001")
        self.assertEqual(can_approve(evs, 2), [])  # no sizes needed when the tech lead is optional
        self.add("gate.approved", {"gate": 2, "approver": "tl", "approver_role": "tech_lead"}, 12)
        self.assertFalse(gate_complete(self.store.events(flow="SP-REQ-001"), 2))  # optional TL does not complete it
        self.add("gate.approved", {"gate": 2, "approver": "m", "approver_role": "delivery_manager"}, 13)
        self.assertTrue(gate_complete(self.store.events(flow="SP-REQ-001"), 2))

    def test_gate_two_with_required_tech_lead_needs_sizes_and_both_people(self):
        from dcp.flowstate import can_approve
        self._to_gate2(["delivery_manager", "tech_lead"])
        self.add("drift.checked", {"passed": True, "blocks": 0}, 10)
        self.add("tickets.linted", {"passed": True, "blocks": 0, "warns": 0, "stage": "ready"}, 11)
        evs = self.store.events(flow="SP-REQ-001")
        self.assertTrue(any("sized" in r for r in can_approve(evs, 2)))
        orders, waiting = self.nxt(12)
        self.assertEqual({w.waiting_for for w in waiting}, {"gate2_delivery_manager", "gate2_tech_lead"})
        self.add("tech.sized", {"sizes": {"s1": "5 days"}}, 12)
        self.assertEqual(can_approve(self.store.events(flow="SP-REQ-001"), 2), [])

    def test_tier_suggestion(self):
        self.assertEqual(suggest_tier("Payment failing on checkout"), "T1")
        self.assertEqual(suggest_tier("Update banner copy"), "T3")
        self.assertEqual(suggest_tier("Add a filter to the PLP"), "T2")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tickets = (EX / "tickets_good.md").read_text()

    def test_creates_come_first_comments_wait_for_keys(self):
        plan = build_plan(self.tickets, "SP-REQ-001", "Requirement Pack SP-REQ-001 v1", "SP", {})
        self.assertEqual({o["op"] for o in plan["ops"]}, {"create"})
        self.assertEqual(len(plan["ops"]), 7)
        titles = [o["title"] for o in plan["ops"]]
        self.assertEqual(titles[0], "EPIC: Returns export")
        self.assertTrue(any(t.startswith("DEP: ") for t in titles))
        self.assertTrue(any(t.startswith("RISK: ") for t in titles))
        self.assertTrue(any(t.startswith("DECISION: ") for t in titles))
        self.assertEqual({o["type"] for o in plan["ops"]}, {"story", "task"})
        for o in plan["ops"]:
            self.assertIn(f"dcp-id: SP-REQ-001/{o['local_id']}", o["description"])
        by_title = {o["title"].split(":")[0]: o for o in plan["ops"] if ":" in o["title"]}
        self.assertEqual(by_title["DEP"]["labels"], ["dependency"])
        self.assertEqual(by_title["RISK"]["labels"], ["risk"])
        self.assertEqual(by_title["DECISION"]["labels"], ["decision"])
        self.assertTrue(by_title["RISK"]["description"].startswith("Label: risk"))  # visible until labels are settable
        self.assertEqual(by_title["EPIC"]["desired_type"], "epic")
        self.assertEqual(plan["ops"][1]["labels"], [])  # stories carry no label

    def test_resume_never_recreates_and_links_resolve(self):
        first = build_plan(self.tickets, "F", "ref", "SP", {})
        mapped = {o["local_id"]: f"SP-{i + 1}" for i, o in enumerate(first["ops"])}
        again = build_plan(self.tickets, "F", "ref", "SP", mapped)
        self.assertEqual({o["op"] for o in again["ops"]}, {"comment"})
        self.assertEqual(again["creates_total"], again["creates_done"])
        for o in again["ops"]:
            self.assertNotIn("{{key", o["body"])
        s2 = next(o for o in again["ops"] if o["local_id"] == "c:s2")
        self.assertIn("Parent epic: SP-1", s2["body"])
        self.assertRegex(s2["body"], r"Blocked by: SP-\d+, SP-\d+")
        partial = {k: v for k, v in mapped.items() if k in ("epic", "s1")}
        some = build_plan(self.tickets, "F", "ref", "SP", partial)
        self.assertNotIn("c:s1", [o["local_id"] for o in some["ops"]])  # still waits for its blocker

    def test_description_round_trips_through_the_linters(self):
        plan = build_plan(self.tickets, "F", "ref", "SP", {})
        items = [{"title": o["title"], "description": o["description"]} for o in plan["ops"]]
        md = pulse_items_to_markdown(items)
        from dcp.ticketlint import parse_tickets
        back = parse_tickets(md)
        orig = parse_tickets(self.tickets)
        self.assertEqual([t.kind for t in back], [t.kind for t in orig])
        story = next(t for t in back if t.kind == "STORY")
        self.assertIn("Given", story.get("acceptance criteria"))
        self.assertTrue(story.get("technical context"))

    def test_sizes_parse_and_apply(self):
        self.assertEqual(parse_sizes("S1=5d s2=3 days S3=4h"), {"s1": "5 days", "s2": "3 days", "s3": "4 hours"})
        self.assertEqual(parse_sizes("nothing here"), {})
        bare = re.sub(r"^Size:.*\n", "", self.tickets, flags=re.M).replace("Status: Closed", "Status: Open")
        res = lint_ready(bare)
        self.assertFalse(res.passed)
        merged = apply_updates(bare, parse_sizes("S1=5d S2=3d S3=1d"), ["Cancelled orders included or not"])
        res = lint_ready(merged)
        self.assertTrue(res.passed, res.render())
        again = apply_updates(merged, parse_sizes("S1=6d"), [])
        self.assertEqual(again.count("Size: 6 days (tech lead)"), 1)
        self.assertEqual(again.count("Size:"), 3)

    def test_sizes_become_comments(self):
        first = build_plan(self.tickets, "F", "ref", "SP", {})
        mapped = {o["local_id"]: f"SP-{i + 1}" for i, o in enumerate(first["ops"])}
        plan = build_plan(self.tickets, "F", "ref", "SP", mapped, sizes={"s1": "5 days"})
        self.assertIn("Size (tech lead): 5 days", [o["body"] for o in plan["ops"]])


def lint_ready(text):
    from datetime import date
    from dcp.ticketlint import lint_tickets
    return lint_tickets(text, RULES, "ready", date(2026, 10, 23))


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.pack = (EX / "pack_good.md").read_text()
        self.tickets = (EX / "tickets_good.md").read_text()
        self.cfg = with_defaults(make_project("SP", "build", "https://pulse.iksula.com/projects/SP/board", "#sp",
                                              "S", "iksula/x", delivery_manager="M:U0EXAMPLE1", tech_lead="A:U0EXAMPLE2"))

    def brief(self, branch="feature/SP-2-returns-export", tier="T2", tickets=None, **kw):
        return build_brief(self.pack, tickets or self.tickets, self.cfg, RULES, "SP-REQ-001", branch, tier, **kw)

    def test_brief_contains_the_documented_requirement(self):
        text, warnings = self.brief()
        for needle in ("Branch: feature/SP-2-returns-export", "Given a store manager is logged in", "40,000 rows",
                       "We must not change the existing returns grid" if False else "Any change to the existing returns grid",
                       "docs/assumptions/SP-REQ-001.md", "/parakh", "Never commit to `main`"):
            self.assertIn(needle, text)
        self.assertTrue(any("pattern_files" in w for w in warnings))

    def test_t1_adds_failure_path_and_rollback(self):
        text, _ = self.brief(tier="T1")
        self.assertIn("failure-path test", text)
        self.assertIn("rollback", text)

    def test_pattern_files_remove_the_warning(self):
        self.cfg["git"]["pattern_files"] = ["app/code/Iksula/Returns/Model/Export.php"]
        text, warnings = self.brief()
        self.assertIn("app/code/Iksula/Returns/Model/Export.php", text)
        self.assertFalse(any("pattern_files" in w for w in warnings))

    def test_refuses_unsafe_or_protected_branches_and_bad_tier(self):
        for branch in ("main", "x; rm -rf /", "-d", "develop", "feature/returns-export", "feature/ML-2-x"):
            with self.assertRaises(BriefError, msg=branch):
                self.brief(branch=branch)
        with self.assertRaises(BriefError):
            self.brief(tier="T9")

    def test_unsized_tickets_build_with_a_warning_unless_the_tech_lead_is_required(self):
        draft = re.sub(r"^Size:.*\n", "", self.tickets, flags=re.M)
        text, _ = self.brief(tickets=draft)  # tech lead optional by default
        self.assertIn("Branch:", text)
        self.cfg["validation"]["require_tech_lead"] = True
        with self.assertRaises(BriefError) as cm:
            self.brief(tickets=draft)
        self.assertIn("not ready", str(cm.exception))

    def test_refuses_open_decisions(self):
        open_dec = self.tickets.replace("Status: Closed", "Status: Open")
        with self.assertRaises(BriefError):
            self.brief(tickets=open_dec)

    def test_refuses_when_tickets_drifted_from_pack(self):
        drifted = self.tickets.replace("Then the export is not visible.", "Then nothing at all happens.")
        with self.assertRaises(BriefError) as cm:
            self.brief(tickets=drifted)
        self.assertIn("drifted", str(cm.exception))

    def test_only_requested_stories(self):
        text, _ = self.brief(only_stories=["Restrict returns export to store managers in Magento admin"])
        self.assertIn("Restrict returns export", text)
        self.assertNotIn("Generate nightly returns CSV job", text)
        with self.assertRaises(BriefError):
            self.brief(only_stories=["No such story"])


class EndToEndTests(unittest.TestCase):
    """One build flow from a Granola note to a launched build, driven through the CLI."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.store = d / "data" / "SP" / "events.jsonl"
        self.base = ["--projects-dir", str(d / "projects"), "--data-dir", str(d / "data")]

    def tearDown(self):
        self.dir.cleanup()

    def cli(self, cmd, *args, project=True):
        extra = (["--project", "SP"] if project else [])
        return run(cmd, *extra, *self.base, *args)

    def log(self, type_, data):
        return run("log", type_, "--store", str(self.store), "--flow", "SP-REQ-001", "--project", "SP",
                   "--actor", "dcp", "--data", json.dumps(data))

    def nxt(self):
        code, out = self.cli("next", "--json")
        self.assertEqual(code, 0, out)
        return json.loads(out)

    def flow_actions(self):
        return [o["action"] for o in self.nxt()["orders"] if o["flow"] == "SP-REQ-001"]

    def test_full_build_flow(self):
        T = str(EX / "transcript.txt")
        # a new call note appears
        self.assertNotIn("poll_granola", actions(self.cli("next", "--json")[1]))  # paste mode: no connector polling
        code, out = self.cli("flow-new", "--source-ref", "g-1", "--title", "SiteOne :: returns export :: SP")
        self.assertEqual((code, json.loads(out)["flow"]), (0, "SP-REQ-001"))
        self.assertEqual(json.loads(self.cli("flow-new", "--source-ref", "g-1", "--title", "x")[1])["status"], "exists")
        code, out = self.cli("flow-new", "--source-ref", "g-9", "--title", "SiteOne :: other :: ML")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["status"], "wrong_project")
        code, out = self.cli("flow-new", "--source-ref", "g-2", "--title", "returns call")
        self.assertEqual(json.loads(out)["status"], "needs_rename")
        self.cli("retitle", "--flow", "SP-REQ-002", "--title", "SiteOne :: returns call :: SP")

        # the call is checked by code first, then drafted
        self.assertEqual(self.flow_actions(), ["assess_call"])
        code, out = self.cli("assess-call", "--flow", "SP-REQ-001", "--transcript", T)
        self.assertEqual(code, 0, out)
        self.assertIn("READY", out)
        # pack
        self.assertIn("draft_pack", self.flow_actions())
        self.log("pack.drafted", {"version": 1})
        self.assertIn("lint_pack", self.flow_actions())
        code, _ = run("lint-pack", str(EX / "pack_good.md"), "--transcript", T, "--log-to", str(self.store),
                                "--flow", "SP-REQ-001", "--project", "SP", "--actor", "dcp")
        self.assertEqual(code, 0)
        acts = self.flow_actions()
        self.assertIn("open_gate", acts)
        self.assertIn("prepare_questions", acts)

        # Gate 1 is a hard stop until opened, and only the delivery manager can approve it
        code, out = self.cli("gate-approve", "--flow", "SP-REQ-001", "--gate", "1", "--approver-id", DM)
        self.assertEqual(code, 1)
        self.assertIn("never opened", out)
        self.cli("gate-open", "--flow", "SP-REQ-001", "--gate", "1")
        code, out = self.cli("gate-approve", "--flow", "SP-REQ-001", "--gate", "1", "--approver-id", TL)
        self.assertEqual(code, 1)
        self.assertIn("cannot approve", out)
        code, out = self.cli("gate-approve", "--flow", "SP-REQ-001", "--gate", "1", "--approver-id", "U0STRANGER")
        self.assertEqual(code, 1)
        self.assertIn("not a registered person", out)
        code, out = self.cli("gate-approve", "--flow", "SP-REQ-001", "--gate", "1", "--approver-id", DM)
        self.assertEqual(code, 1)
        self.assertIn("open questions have not been sent", out)
        self.log("questions.sent", {"count": 3})
        self.assertNotIn("prepare_questions", self.flow_actions())
        code, out = self.cli("gate-approve", "--flow", "SP-REQ-001", "--gate", "1", "--approver-id", DM)
        self.assertEqual(code, 0, out)

        # tickets drafted, checked, created in Pulse
        self.assertEqual(self.flow_actions()[0], "draft_tickets")
        self.log("tickets.drafted", {"count": 7})
        self.assertIn("lint_tickets", self.flow_actions())
        tickets = self.d / "tickets.md"
        sized = (EX / "tickets_good.md").read_text()
        tickets.write_text(re.sub(r"^Size:.*\n", "", sized, flags=re.M).replace("Status: Closed", "Status: Open"))
        code, out = run("lint-tickets", str(tickets), "--stage", "draft", "--today", "2026-10-23",
                                  "--log-to", str(self.store), "--flow", "SP-REQ-001", "--project", "SP", "--actor", "dcp")
        self.assertEqual(code, 0, out)
        self.assertIn("check_drift", self.flow_actions())
        code, _ = run("check-drift", str(EX / "pack_good.md"), str(tickets), "--log-to", str(self.store),
                                "--flow", "SP-REQ-001", "--project", "SP", "--actor", "dcp")
        self.assertEqual(code, 0)
        self.assertEqual(self.flow_actions(), ["create_pulse_tickets"])

        code, out = self.cli("tickets-plan", "--flow", "SP-REQ-001", "--tickets", str(tickets))
        plan = json.loads(out)
        self.assertEqual(len(plan["ops"]), 7)
        for i, op in enumerate(plan["ops"], 1):
            self.cli("tickets-record", "--flow", "SP-REQ-001", "--local-id", op["local_id"], "--key", f"SP-{i}")
        plan = json.loads(self.cli("tickets-plan", "--flow", "SP-REQ-001", "--tickets", str(tickets))[1])
        self.assertEqual({o["op"] for o in plan["ops"]}, {"comment"})
        for op in plan["ops"]:
            self.cli("tickets-record", "--flow", "SP-REQ-001", "--local-id", op["local_id"], "--key", op["on_key"])
        plan = json.loads(self.cli("tickets-plan", "--flow", "SP-REQ-001", "--tickets", str(tickets))[1])
        self.assertTrue(plan["complete"])
        created = [e for e in EventStore(self.store).events(flow="SP-REQ-001") if e.type == "tickets.created"]
        self.assertEqual(len(created), 1)  # logged once, even though tickets-plan ran three times

        # Gate 2 needs sizes, the ready check and a decided open decision
        self.assertEqual(self.flow_actions(), ["open_gate"])
        self.cli("gate-open", "--flow", "SP-REQ-001", "--gate", "2")
        code, out = self.cli("gate-approve", "--flow", "SP-REQ-001", "--gate", "2", "--approver-id", TL)
        self.assertEqual(code, 1)
        self.assertIn("ready-stage", out)
        # the tech lead is optional: close the open decision, no sizes needed, run the ready check
        code, out = self.cli("tickets-size", "--flow", "SP-REQ-001", "--tickets", str(tickets),
                             "--close", "Cancelled orders included or not", "--by-id", DM)
        self.assertEqual(code, 0, out)
        self.assertIn("lint_tickets", self.flow_actions())
        code, out = run("lint-tickets", str(tickets), "--stage", "ready", "--size-optional", "--today", "2026-10-23",
                        "--log-to", str(self.store), "--flow", "SP-REQ-001", "--project", "SP", "--actor", "dcp")
        self.assertEqual(code, 0, out)
        # an optional tech lead (here via the delegate) may validate, but that does not complete the gate
        code, out = self.cli("gate-approve", "--flow", "SP-REQ-001", "--gate", "2", "--approver-id", DELEGATE)
        self.assertEqual(code, 0, out)
        self.assertIn("as delegate", out)
        self.assertIn("still needs delivery_manager", out)
        self.assertIn("gate2_delivery_manager", {w["waiting_for"] for w in self.nxt()["waiting"]})
        self.assertNotIn("tier", {w["waiting_for"] for w in self.nxt()["waiting"]})
        code, out = self.cli("gate-approve", "--flow", "SP-REQ-001", "--gate", "2", "--approver-id", DM)
        self.assertEqual(code, 0, out)
        self.assertIn("gate complete", out)

        # tier, then the branch, then the build
        waiting = {w["waiting_for"] for w in self.nxt()["waiting"]}
        self.assertIn("tier", waiting)
        code, out = self.cli("tier-set", "--flow", "SP-REQ-001", "--tier", "T2", "--setter-id", DM)
        self.assertEqual(code, 1)
        self.assertIn("Leads set it", out)
        self.assertEqual(self.cli("tier-set", "--flow", "SP-REQ-001", "--tier", "T2", "--setter-id", TL)[0], 0)
        self.assertIn("branch", {w["waiting_for"] for w in self.nxt()["waiting"]})
        for bad in ("main", "-rf", "x;rm -rf /", "a..b", "feature/returns-export", "feature/SP-99-nope",
                    "feature/SP-6-a-risk-is-not-a-story", "feature/ML-2-wrong-project"):
            code, _ = self.cli("branch-share", "--flow", "SP-REQ-001", f"--branch={bad}", "--actor", "Arjun K")
            self.assertEqual(code, 1, bad)
        code, out = self.cli("build-brief", "--flow", "SP-REQ-001", "--pack", str(EX / "pack_good.md"),
                             "--tickets", str(tickets))
        self.assertEqual(code, 1)
        self.assertIn("no branch shared", out)
        code, out = self.cli("branch-name", "--flow", "SP-REQ-001", "--key", "SP-2",
                             "--title", "Add returns export button with date and store filters")
        self.assertEqual((code, out.strip()), (0, "feature/SP-2-add-returns-export-button-with-date"))
        self.assertEqual(self.cli("branch-name", "--flow", "SP-REQ-001", "--key", "SP-6", "--title", "x")[0], 1)
        code, out = self.cli("branch-share", "--flow", "SP-REQ-001", "--branch", "feature/SP-2-returns-export",
                             "--actor", "Arjun K")
        self.assertEqual(code, 0, out)
        self.assertIn("git checkout feature/SP-2-returns-export", out)
        orders = [o for o in self.nxt()["orders"] if o["action"] == "launch_build"]
        self.assertEqual(orders[0]["inputs"]["branch"], "feature/SP-2-returns-export")
        brief_path = self.d / "briefs" / "SP-REQ-001.md"
        code, out = self.cli("build-brief", "--flow", "SP-REQ-001", "--pack", str(EX / "pack_good.md"),
                             "--tickets", str(tickets), "--out", str(brief_path))
        self.assertEqual(code, 0, out)
        text = brief_path.read_text()
        self.assertIn("feature/SP-2-returns-export", text)
        self.assertIn("SP-2", text)  # Pulse keys are carried into the brief
        self.log("build.started", {"ticket": "SP-2"})
        self.assertNotIn("launch_build", self.flow_actions())
        self.assertEqual(EventStore(self.store).verify(), [])

    def test_brief_blocked_before_gates(self):
        self.cli("flow-new", "--source-ref", "g-1", "--title", "SiteOne :: returns export :: SP")
        self.log("pulse.mapped", {"local_id": "s1", "key": "SP-2"})
        self.log("branch.shared", {"repo": "iksula/siteone-pimcore", "branch": "feature/SP-2-returns-export"})
        code, out = self.cli("build-brief", "--flow", "SP-REQ-001", "--pack", str(EX / "pack_good.md"),
                             "--tickets", str(EX / "tickets_good.md"))
        self.assertEqual(code, 1)
        self.assertIn("Gate 1 and Gate 2", out)

    def test_incomplete_project_refuses_to_run(self):
        shutil.copy(ROOT / "projects" / "ML.json", self.d / "projects" / "ML.json")
        code, out = run("next", "--project", "ML", *self.base)
        self.assertEqual(code, 1)
        self.assertIn("incomplete", out)


class SizingTests(unittest.TestCase):
    """Sizing is a separate, optional, tech-lead step: after the tickets exist, before development starts."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.base = ["--projects-dir", str(d / "projects"), "--data-dir", str(d / "data")]
        self.store = EventStore(d / "data" / "SP" / "events.jsonl")
        self.tickets = d / "tickets.md"
        self.tickets.write_text(re.sub(r"^Size:.*\n", "", (EX / "tickets_good.md").read_text(), flags=re.M))

    def tearDown(self):
        self.dir.cleanup()

    def add(self, type_, data):
        self.store.append(type_, "SP-REQ-001", "SP", "t", data)

    def size(self, who, sizes="S1=5d S2=3d S3=1d"):
        return run("tickets-size", "--project", "SP", *self.base, "--flow", "SP-REQ-001", "--tickets", str(self.tickets),
                   "--sizes", sizes, "--by-id", who)

    def created(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("pack.drafted", {"version": 1})
        self.add("pack.linted", {"version": 1, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0})
        self.add("gate.opened", {"gate": 1, "pack_version": 1})
        self.add("gate.approved", {"gate": 1, "approver": "m", "approver_role": "delivery_manager"})
        self.add("tickets.drafted", {"count": 7})
        self.add("tickets.linted", {"passed": True, "blocks": 0, "warns": 0, "stage": "draft"})
        self.add("drift.checked", {"passed": True, "blocks": 0})
        self.add("tickets.created", {"count": 7})

    def waiting(self):
        return json.loads(run("next", "--project", "SP", *self.base, "--json")[1])["waiting"]

    def test_nothing_to_size_before_tickets_exist(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        code, out = self.size(TL)
        self.assertEqual(code, 1)
        self.assertIn("nothing to size", out)

    def test_only_the_tech_lead_or_their_delegate_can_size(self):
        self.created()
        code, out = self.size(DM)
        self.assertEqual(code, 1)
        self.assertIn("Sizes come from the tech lead", out)
        self.assertEqual(self.size("U0STRANGER")[0], 1)
        self.assertEqual(self.size(DELEGATE, "S1=2d")[0], 0)  # the delegate covers the tech lead
        self.assertEqual(self.size(TL)[0], 0)
        sized = [e for e in self.store.events(flow="SP-REQ-001") if e.type == "tech.sized"]
        self.assertEqual([e.data["by_role"] for e in sized], ["delegate", "tech_lead"])
        self.assertIn("Size: 5 days (tech lead)", self.tickets.read_text())

    def test_sizing_closes_when_development_starts(self):
        self.created()
        self.add("build.started", {"ticket": "SP-2"})
        code, out = self.size(TL)
        self.assertEqual(code, 1)
        self.assertIn("sizing is closed", out)

    def test_bad_sizes_are_refused(self):
        self.created()
        code, out = self.size(TL, "five days")
        self.assertEqual(code, 1)
        self.assertIn("no sizes understood", out)

    def test_optional_wait_is_visible_never_blocks_and_clears_once_sized(self):
        self.created()
        w = [x for x in self.waiting() if x["waiting_for"] == "sizes"]
        self.assertEqual((w[0]["owner"], w[0]["optional"]), ("tech_lead", True))
        self.size(TL)
        self.assertEqual([x for x in self.waiting() if x["waiting_for"] == "sizes"], [])

    def test_optional_wait_is_never_nudged(self):
        self.created()
        far = "2099-01-01T00:00:00Z"
        data = json.loads(run("next", "--project", "SP", *self.base, "--json", "--now", far)[1])
        self.assertEqual([o for o in data["orders"] if o["action"] == "nudge" and o["inputs"]["check"] == "sizes"], [])

    def test_unsized_build_is_flagged_not_blocked(self):
        from dcp.brief import build_brief as bb
        cfg = with_defaults(load_project(ROOT / "projects" / "SP.json"))
        pack = (EX / "pack_good.md").read_text()
        text, warnings = bb(pack, self.tickets.read_text(), cfg, RULES, "SP-REQ-001", "feature/SP-2-returns-export", "T2")
        self.assertTrue(any("Not sized by the tech lead" in w for w in warnings))
        sized = (EX / "tickets_good.md").read_text()
        _, warnings = bb(pack, sized, cfg, RULES, "SP-REQ-001", "feature/SP-2-returns-export", "T2")
        self.assertFalse(any("Not sized" in w for w in warnings))

    def test_launch_order_says_whether_the_build_is_unsized(self):
        self.created()
        self.add("gate.opened", {"gate": 2, "required_roles": ["delivery_manager"]})
        self.add("gate.approved", {"gate": 2, "approver": "m", "approver_role": "delivery_manager"})
        self.add("tier.set", {"tier": "T2", "actor_role": "tech_lead"})
        self.add("branch.shared", {"repo": "iksula/x", "branch": "feature/SP-2-x"})
        launch = lambda: next(o for o in json.loads(run("next", "--project", "SP", *self.base, "--json")[1])["orders"]  # noqa: E731
                              if o["action"] == "launch_build")
        self.assertTrue(launch()["inputs"]["unsized"])
        self.add("tech.sized", {"sizes": {"s1": "5 days"}})
        self.assertFalse(launch()["inputs"]["unsized"])

    def test_sizing_check_is_informational_and_never_changes_the_score(self):
        from dcp.scoring import evaluate_store
        self.add("call.recorded", {"title": "ACME :: t :: SP"})
        before = evaluate_store(self.store, RULES, flow="SP-REQ-001")[0]
        self.add("tickets.created", {"count": 7})
        self.add("build.started", {"ticket": "SP-2"})
        r = evaluate_store(self.store, RULES, flow="SP-REQ-001")[0]
        c20 = next(c for c in r.checks if c.id == "C20")
        self.assertEqual(c20.status, "fail")  # visible
        self.assertEqual(c20.weight, 0)       # but free
        no20 = [c for c in r.checks if c.id != "C20"]
        earned = sum(c.weight for c in no20 if c.status == "pass"); due = sum(c.weight for c in no20 if c.status in ("pass", "fail"))
        self.assertEqual(r.score, round(earned / due * 100, 1))


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        cfg = load_project(ROOT / "projects" / "ML.json")
        cfg["slack"] = {"channel": "#ml-support", "intake_channel": "#ml-support"}
        cfg["people"] = {"tech_lead": {"name": "Arjun K", "slack_id": TL, "email": ""}}
        (d / "projects" / "ML.json").write_text(json.dumps(cfg))
        self.base = ["--projects-dir", str(d / "projects"), "--data-dir", str(d / "data")]
        self.d = d

    def tearDown(self):
        self.dir.cleanup()

    def test_support_request_to_ticket_to_branch(self):
        code, out = run("flow-new", "--project", "ML", *self.base, "--source-ref", "https://slack/p1",
                        "--text", "Payment failing on checkout for guest users since 9am", "--title", "payment failing")
        flow = json.loads(out)["flow"]
        self.assertEqual(flow, "ML-SUP-001")
        orders = json.loads(run("next", "--project", "ML", *self.base, "--json")[1])["orders"]
        o = next(x for x in orders if x["action"] == "create_support_ticket")
        self.assertEqual(o["inputs"]["suggested_tier"], "T1")
        self.assertEqual(o["inputs"]["parent"], "ML-1454")
        self.assertEqual(o["inputs"]["title_prefix"], "[L2] :")
        self.assertIn("poll_slack", [x["action"] for x in orders])
        self.assertNotIn("poll_granola", [x["action"] for x in orders])  # support has no call notes

        fields = {"what_happened": "Guests cannot pay; gateway returns an error", "impact": "All guest orders since 9am",
                  "environment": "Production", "evidence": "Order ids 3011000051900-3011000051910",
                  "reported_by": "Priya", "slack_thread": "https://slack/p1", "tier": "T1", "next_step": "Check gateway logs"}
        f = self.d / "fields.json"
        f.write_text(json.dumps(fields))
        code, out = run("support-ticket", "--project", "ML", *self.base, "--flow", flow, "--fields", str(f),
                        "--title", "Guest payment failing at checkout")
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["create"]["title"], "[L2] : Guest payment failing at checkout")
        self.assertEqual(payload["create"]["priority"], "critical")
        self.assertEqual(payload["create"]["projectKey"], "ML")
        self.assertIn("Impact: All guest orders", payload["create"]["description"])
        self.assertIn("ML-1454", payload["parent_note"])

        fields.pop("impact")
        f.write_text(json.dumps(fields))
        code, out = run("support-ticket", "--project", "ML", *self.base, "--flow", flow, "--fields", str(f),
                        "--title", "Guest payment failing at checkout")
        self.assertEqual(code, 1)
        self.assertIn("Missing 'Impact'", out)

        store = EventStore(self.d / "data" / "ML" / "events.jsonl")
        store.append("ticket.created", flow, "ML", "dcp", {"ticket": "ML-1530"}, track="support")
        waiting = json.loads(run("next", "--project", "ML", *self.base, "--json")[1])["waiting"]
        self.assertEqual(waiting[0]["waiting_for"], "tier")
        self.assertEqual(run("tier-set", "--project", "ML", *self.base, "--flow", flow, "--tier", "T1", "--setter-id", TL)[0], 0)
        waiting = json.loads(run("next", "--project", "ML", *self.base, "--json")[1])["waiting"]
        self.assertEqual(waiting[0]["waiting_for"], "branch")
        for bad in ("feature/ML-9-other-ticket", "fix/ML-1530-x", "main"):
            self.assertEqual(run("branch-share", "--project", "ML", *self.base, "--flow", flow, f"--branch={bad}",
                                 "--repo", "iksula/lego-magento", "--actor", "Arjun K")[0], 1, bad)
        code, out = run("branch-share", "--project", "ML", *self.base, "--flow", flow,
                        "--branch", "bugfix/ML-1530-guest-payment", "--repo", "iksula/lego-magento", "--actor", "Arjun K")
        self.assertEqual(code, 0, out)
        # many L2 tickets need no code at all (access, config in admin): one command clears the wait
        run("no-code", "--project", "ML", *self.base, "--flow", flow, "--reason", "fixed in Magento admin", "--actor", "Arjun K")
        data = json.loads(run("next", "--project", "ML", *self.base, "--json")[1])
        self.assertEqual([w for w in data["waiting"] if w["flow"] == flow], [])

    def test_support_sweep_lints_live_board_tickets(self):
        export = [{"key": "ML-1", "title": "Returns export button", "description": "Why: x"}]
        f = self.d / "export.json"
        f.write_text(json.dumps(export))
        code, out = run("lint-pulse", "--project", "ML", *self.base, "--export", str(f))
        self.assertEqual(code, 1)
        self.assertIn("TK", out)


if __name__ == "__main__":
    unittest.main()
