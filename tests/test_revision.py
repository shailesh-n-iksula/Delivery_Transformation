import contextlib
import io
import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dcp.cli import main
from dcp.config import load_rules
from dcp.events import open_store
from dcp.flowstate import can_approve, epoch_events, gate_complete, next_orders
from dcp.project import load_project
from dcp.pulseplan import build_plan, hashes_from_events, mapped_from_events, titles_from_events

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
RULES = load_rules()
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
DM, TL = "U0EXAMPLE1", "U0EXAMPLE2"
CALL = (EX / "transcript.txt").read_text()
FLOW = "SP-REQ-001"


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


class FlowBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.base = ["--projects-dir", str(d / "projects"), "--data-dir", str(d / "data")]
        self.store = open_store(d / "data" / "SP" / "events.jsonl")
        self.cfg = load_project(d / "projects" / "SP.json")
        self.n = 0

    def tearDown(self):
        self.dir.cleanup()

    def add(self, type_, data, flow=FLOW):
        # live timestamps, like the commands use; events written in the same second keep their file order
        return self.store.append(type_, flow, "SP", "t", data)

    def orders(self):
        o, w = next_orders(self.store, self.cfg, datetime.now(timezone.utc) + timedelta(days=1), RULES)
        return {x.action: x for x in o if x.flow == FLOW}, [x for x in w if x.flow == FLOW]

    def acts(self):
        return sorted(self.orders()[0])

    def waits(self):
        return sorted(w.waiting_for for w in self.orders()[1])

    def approved_through_gate2(self, build_started=False):
        for st in ("granola", "pulse_sweep", "governance", "slack"):
            self.add("poll.ran", {"stage": st}, flow="_poll")
        self.add("call.recorded", {"title": "S :: t :: SP"})
        self.add("call.assessed", {"covered": ["a"] * 6, "missing": [], "ready": True, "needed": 5})
        self.add("pack.drafted", {"version": 1})
        self.add("pack.linted", {"version": 1, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0})
        self.add("gate.opened", {"gate": 1, "pack_version": 1, "required_roles": ["delivery_manager"]})
        self.add("gate.approved", {"gate": 1, "approver": "Meera N", "approver_role": "delivery_manager", "pack_version": 1})
        self.add("tickets.drafted", {"count": 7})
        self.add("tickets.linted", {"passed": True, "blocks": 0, "warns": 0, "stage": "draft"})
        self.add("drift.checked", {"passed": True, "blocks": 0})
        self.add("tickets.created", {"count": 7})
        self.add("gate.opened", {"gate": 2, "required_roles": ["delivery_manager"]})
        self.add("tickets.linted", {"passed": True, "blocks": 0, "warns": 0, "stage": "ready"})
        self.add("gate.approved", {"gate": 2, "approver": "Meera N", "approver_role": "delivery_manager"})
        self.add("tier.set", {"tier": "T2", "actor_role": "tech_lead"})
        self.add("branch.shared", {"repo": "iksula/x", "branch": "feature/SP-2-x"})
        if build_started:
            self.add("build.started", {"ticket": "SP-2"})

    def cr(self, who=DM, reason="the client now wants the export to include cancelled orders"):
        return run("cr-open", "--project", "SP", *self.base, "--flow", FLOW, "--reason", reason, "--by-id", who)


class ChangeRequestTests(FlowBase):
    def test_guards(self):
        self.add("call.recorded", {"title": "S :: t :: SP"})
        code, out = self.cr()
        self.assertEqual(code, 1)
        self.assertIn("no pack to change", out)
        self.approved_through_gate2()
        self.assertEqual(self.cr(who=TL)[0], 1)
        self.assertEqual(self.cr(who="U0NOBODY99")[0], 1)
        self.assertEqual(self.cr(reason="change")[0], 1)
        self.assertEqual(self.store.events(type="pack.reopened"), [])

    def test_a_request_is_recorded_as_rework_and_reopens_the_pack(self):
        self.approved_through_gate2()
        self.assertEqual(self.cr()[0], 0)
        self.assertEqual(len(self.store.events(type="change_request.raised")), 1)
        re_ = self.store.events(type="pack.reopened")[0]
        self.assertEqual((re_.data["from_version"], re_.actor), (1, "Meera N"))
        from dcp.scoring import evaluate_store
        self.assertEqual(evaluate_store(self.store, RULES, flow=FLOW, now=T0 + timedelta(days=3))[0].metrics["change_requests"], 1)
        code, out = self.cr()
        self.assertEqual(code, 1)
        self.assertIn("already open", out)

    def test_the_only_thing_due_is_revising_the_pack_on_a_drafting_model(self):
        self.approved_through_gate2()
        self.cr()
        orders, waiting = self.orders()
        self.assertEqual(sorted(orders), ["revise_pack"])
        o = orders["revise_pack"]
        self.assertEqual((o.inputs["version"], o.model, o.needs_ai), (2, "sonnet", True))
        self.assertEqual(waiting, [])     # nobody is blocked on the old gates any more

    def test_old_approvals_cannot_be_reused_for_the_new_version(self):
        self.approved_through_gate2()
        self.cr()
        evs = self.store.events(flow=FLOW)
        self.assertEqual(epoch_events(evs), [])                     # nothing counts until the revised pack exists
        self.add("pack.drafted", {"version": 2})
        self.add("pack.linted", {"version": 2, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0})
        evs = self.store.events(flow=FLOW)
        self.assertFalse(gate_complete(evs, 2))
        self.assertTrue(any("never opened" in r for r in can_approve(evs, 1)))
        self.assertTrue(any("not been created" in r for r in can_approve(evs, 2)))

    def test_the_whole_cycle_runs_again_against_the_new_version(self):
        self.approved_through_gate2()
        self.cr()
        self.add("pack.drafted", {"version": 2})
        self.assertEqual(self.acts(), ["lint_pack"])
        self.add("pack.linted", {"version": 2, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0})
        o = self.orders()[0]["open_gate"]
        self.assertEqual(o.inputs, {"gate": 1, "pack_version": 2})
        # approving through the command records which version was approved
        run("gate-open", "--project", "SP", *self.base, "--flow", FLOW, "--gate", "1")
        code, out = run("gate-approve", "--project", "SP", *self.base, "--flow", FLOW, "--gate", "1", "--approver-id", DM)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.events(type="gate.approved")[-1].data["pack_version"], 2)
        orders, _ = self.orders()
        self.assertEqual(sorted(orders), ["revise_tickets"])              # tickets exist, so revise, not draft
        self.assertIn("stage3b_revise_tickets", orders["revise_tickets"].inputs["prompt"])
        self.add("tickets.drafted", {"count": 8})
        self.assertEqual(self.acts(), ["lint_tickets"])
        self.add("tickets.linted", {"passed": True, "blocks": 0, "warns": 0, "stage": "draft"})
        self.assertEqual(self.acts(), ["check_drift"])
        self.add("drift.checked", {"passed": True, "blocks": 0})
        o = self.orders()[0]["create_pulse_tickets"]
        self.assertIn("apply the changes", o.why)
        self.add("tickets.created", {"count": 8})
        self.assertEqual(self.acts(), ["open_gate"])                      # Gate 2 again
        run("gate-open", "--project", "SP", *self.base, "--flow", FLOW, "--gate", "2")
        self.assertEqual(self.waits()[0], "gate2_delivery_manager")
        self.add("tickets.linted", {"passed": True, "blocks": 0, "warns": 0, "stage": "ready"})
        run("gate-approve", "--project", "SP", *self.base, "--flow", FLOW, "--gate", "2", "--approver-id", DM)
        self.assertNotIn("gate2_delivery_manager", self.waits())

    def test_a_redraft_after_a_failed_check_does_not_inherit_an_approval_of_an_older_version(self):
        self.approved_through_gate2()
        self.add("pack.drafted", {"version": 2})          # drafted again with no change request in between
        self.add("pack.linted", {"version": 2, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0})
        o = self.orders()[0]
        self.assertIn("open_gate", o)

    def test_the_developer_is_told_once_if_the_build_had_already_started(self):
        self.approved_through_gate2(build_started=True)
        self.cr()
        self.add("pack.drafted", {"version": 2})
        self.add("pack.linted", {"version": 2, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0})
        self.add("gate.opened", {"gate": 1, "pack_version": 2})
        self.add("gate.approved", {"gate": 1, "approver": "Meera N", "approver_role": "delivery_manager", "pack_version": 2})
        self.add("tickets.drafted", {"count": 8})
        self.add("tickets.linted", {"passed": True, "blocks": 0, "warns": 0, "stage": "draft"})
        self.add("drift.checked", {"passed": True, "blocks": 0})
        self.add("tickets.created", {"count": 8})
        self.add("gate.opened", {"gate": 2, "required_roles": ["delivery_manager"]})
        self.add("gate.approved", {"gate": 2, "approver": "Meera N", "approver_role": "delivery_manager"})
        o = self.orders()[0]
        self.assertIn("notify_change", o)
        self.assertEqual(o["notify_change"].inputs["pack_version"], 2)
        self.add("nudge.sent", {"check": "cr_notify"})
        self.assertNotIn("notify_change", self.orders()[0])

    def test_no_notice_if_the_build_had_not_started(self):
        self.approved_through_gate2(build_started=False)
        self.cr()
        self.add("pack.drafted", {"version": 2})
        self.add("pack.linted", {"version": 2, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0})
        self.add("gate.opened", {"gate": 1, "pack_version": 2})
        self.add("gate.approved", {"gate": 1, "approver": "Meera N", "approver_role": "delivery_manager", "pack_version": 2})
        self.add("tickets.drafted", {"count": 8})
        self.add("tickets.linted", {"passed": True, "blocks": 0, "warns": 0, "stage": "draft"})
        self.add("drift.checked", {"passed": True, "blocks": 0})
        self.add("tickets.created", {"count": 8})
        self.add("gate.opened", {"gate": 2, "required_roles": ["delivery_manager"]})
        self.add("gate.approved", {"gate": 2, "approver": "Meera N", "approver_role": "delivery_manager"})
        self.assertNotIn("notify_change", self.orders()[0])


class FollowUpCallTests(FlowBase):
    def setUp(self):
        super().setUp()
        self.inbox = self.d / "data" / "SP" / "inbox"
        self.inbox.mkdir(parents=True)
        for st in ("pulse_sweep", "governance"):
            self.add("poll.ran", {"stage": st}, flow="_poll")
        (self.inbox / "first.txt").write_text("Title: SiteOne :: returns export :: SP\n" + CALL)
        self.ingest()

    def ingest(self):
        return json.loads(run("ingest", "--project", "SP", *self.base, "--json")[1])

    def follow(self, text=None, flow=FLOW, name="second.txt"):
        body = text or ("Me: " + "Priya said the export must also include cancelled orders, and finance wants a status column. " * 8)
        (self.inbox / name).write_text(f"Meeting Title: Follow-up :: returns export :: SP\nDate: Oct 20\nFlow: {flow}\n\nTranscript:\n" + body)
        return self.ingest()

    def transcript(self):
        return (self.d / "data" / "SP" / FLOW / "transcript.txt").read_text()

    def test_a_follow_up_is_added_to_its_flow_and_the_call_is_assessed_again(self):
        run("assess-call", "--project", "SP", *self.base, "--flow", FLOW)
        self.assertEqual(self.acts(), ["draft_pack"])
        r = self.follow()
        self.assertEqual(r["attached"][0]["flow"], FLOW)
        self.assertFalse(r["attached"][0]["reopened"])
        t = self.transcript()
        self.assertIn("=== FOLLOW-UP CALL: Follow-up :: returns export :: SP (Oct 20) ===", t)
        self.assertIn("cancelled orders", t)
        self.assertTrue(t.startswith("Meera (Iksula)"))                    # the first call is untouched
        self.assertNotIn("Flow:", t)                                        # header lines never reach the transcript
        self.assertEqual(self.acts(), ["assess_call"])                     # nothing is drafted from a stale assessment
        self.assertEqual(len(self.store.flows("SP")), 1)                   # not a second flow

    def test_with_a_pack_already_drafted_the_pack_is_reopened(self):
        self.add("call.assessed", {"covered": ["a"] * 6, "missing": [], "ready": True, "needed": 5})
        self.add("pack.drafted", {"version": 1})
        self.add("pack.linted", {"version": 1, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0})
        r = self.follow()
        self.assertTrue(r["attached"][0]["reopened"])
        re_ = self.store.events(type="pack.reopened")[0]
        self.assertTrue(re_.data["reason"].startswith("follow-up call"))
        self.assertEqual(self.acts(), ["revise_pack"])

    def test_the_same_follow_up_twice_is_recognised(self):
        self.follow(name="a.txt")
        r = self.follow(name="b.txt")
        self.assertEqual((r["attached"], r["duplicates"][0]["flow"]), ([], FLOW))
        self.assertEqual(self.transcript().count("=== FOLLOW-UP CALL"), 1)

    def test_an_unknown_flow_is_refused_and_stays_in_the_inbox(self):
        r = self.follow(flow="SP-REQ-099", name="x.txt")
        self.assertEqual(r["attached"], [])
        self.assertIn("no call recorded", r["rejected"][0]["reason"])
        self.assertTrue((self.inbox / "x.txt").exists())

    def test_the_flow_header_is_case_insensitive_and_a_summary_is_still_refused(self):
        r = self.follow(flow="sp-req-001")
        self.assertEqual(r["attached"][0]["flow"], FLOW)
        summary = "# Notes\n" + "\n".join(f"- point {i} about the discussion and what was agreed with the client team today" for i in range(14))
        r = self.follow(text=summary, name="s.txt")
        self.assertIn("AI summary", r["rejected"][0]["reason"])


class PlanChangeTests(unittest.TestCase):
    TICKETS = (EX / "tickets_good.md").read_text()

    def settle(self, md, mapped=None, hashes=None, titles=None, version=2):
        """Executes the plan the way the runner does, until it is complete."""
        mapped, hashes, titles = dict(mapped or {}), dict(hashes or {}), dict(titles or {})
        n = 100 + len(mapped)
        for _ in range(8):
            plan = build_plan(md, "F", "Requirement Pack F v%d" % version, "SP", mapped, hashes=hashes, titles=titles, version=version)
            if plan["complete"] and not plan["ops"]:
                return plan, mapped, hashes, titles
            for op in plan["ops"]:
                n += 1
                mapped[op["local_id"]] = op.get("on_key") or f"SP-{n}"
                if op["op"] == "create":
                    hashes[op["local_id"]], titles[op["local_id"]] = op["hash"], op["title"]
                elif op["local_id"].startswith("chg:"):
                    lid, h = op["local_id"].split(":")[1:3]
                    hashes[lid] = h
        raise AssertionError("plan never settled")

    def test_an_unchanged_set_plans_nothing(self):
        plan, m, h, t = self.settle(self.TICKETS, version=1)
        again = build_plan(self.TICKETS, "F", "ref", "SP", m, hashes=h, titles=t, version=2)
        self.assertEqual((again["ops"], again["complete"], again["warnings"]), ([], True, []))

    def test_a_changed_ticket_gets_a_change_note_with_the_new_text_ready_for_an_update_call(self):
        _, m, h, t = self.settle(self.TICKETS, version=1)
        changed = self.TICKETS.replace("Then the two counts match.", "Then the two counts match and cancelled orders are listed.")
        plan = build_plan(changed, "F", "ref", "SP", m, hashes=h, titles=t, version=2)
        self.assertEqual(len(plan["ops"]), 1)
        op = plan["ops"][0]
        self.assertTrue(op["local_id"].startswith("chg:s1:"))
        self.assertEqual(op["on_key"], m["s1"])
        self.assertIn("Requirement changed (pack v2)", op["body"])
        self.assertIn("cancelled orders are listed", op["body"])
        self.assertTrue(op["needs_api_update"])
        self.assertIn("cancelled orders are listed", op["update_to"]["description"])

    def test_a_recorded_change_is_not_planned_twice_and_survives_replanning(self):
        _, m, h, t = self.settle(self.TICKETS, version=1)
        changed = self.TICKETS.replace("Then the two counts match.", "Then the two counts match and cancelled orders are listed.")
        plan, m2, h2, t2 = self.settle(changed, m, h, t)
        self.assertTrue(plan["complete"])
        again = build_plan(changed, "F", "ref", "SP", m2, hashes=h2, titles=t2, version=2)
        self.assertEqual(again["ops"], [])

    def test_a_pack_version_bump_alone_is_not_a_change_to_any_ticket(self):
        _, m, h, t = self.settle(self.TICKETS, version=1)
        bumped = self.TICKETS.replace("Requirement Pack SP-REQ-001 v1", "Requirement Pack SP-REQ-001 v2")
        self.assertNotEqual(bumped, self.TICKETS)
        plan = build_plan(bumped, "F", "ref", "SP", m, hashes=h, titles=t, version=2)
        self.assertEqual([o["local_id"] for o in plan["ops"] if o["local_id"].startswith("chg:")], [])
        # but a real content change in the same set still is one
        real = bumped.replace("Then the two counts match.", "Then the two counts match exactly.")
        plan = build_plan(real, "F", "ref", "SP", m, hashes=h, titles=t, version=2)
        self.assertEqual([o["local_id"].split(":")[1] for o in plan["ops"] if o["local_id"].startswith("chg:")], ["s1"])

    def test_new_tickets_added_at_the_end_are_created_and_linked_without_touching_the_rest(self):
        _, m, h, t = self.settle(self.TICKETS, version=1)
        extra = self.TICKETS.replace("### DEP: Client to confirm the SFTP target", """### STORY: Add a status column to the returns export for finance
Why: Finance wants to see the status of each return.
Scope in:
1. Status column.
Scope out:
1. None
Acceptance criteria:
1. Given the export has run, When finance opens the file, Then each row shows the return status.
Client's words: "finance wants a status column"
Technical context: Magento admin export of up to 40,000 rows a month.
Links: Requirement Pack F v2; Granola call x; Epic: Returns export
Answers from the client: None
Assumptions: None
Blocked by: none

### DEP: Client to confirm the SFTP target""")
        plan = build_plan(extra, "F", "ref", "SP", m, hashes=h, titles=t, version=2)
        self.assertEqual([o["local_id"] for o in plan["ops"] if o["op"] == "create"], ["s4"])   # only the new story
        self.assertFalse([o for o in plan["ops"] if o["local_id"].startswith(("chg:", "rm:"))])  # nothing else is touched
        self.assertEqual(plan["warnings"], [])
        plan2, m2, h2, t2 = self.settle(extra, m, h, t)
        self.assertTrue(plan2["complete"])
        self.assertIn("c:s4", m2)

    def test_inserting_in_the_middle_is_caught_as_a_reorder(self):
        _, m, h, t = self.settle(self.TICKETS, version=1)
        swapped = self.TICKETS.replace("Generate nightly returns CSV job and deliver it to SFTP for finance", "ZZZ placeholder different story entirely")
        plan = build_plan(swapped, "F", "ref", "SP", m, hashes=h, titles=t, version=2)
        self.assertTrue(any("keep their order" in w for w in plan["warnings"]))

    def test_a_removed_story_gets_a_removal_note_and_only_once(self):
        _, m, h, t = self.settle(self.TICKETS, version=1)
        gone = self.TICKETS + "\n### REMOVED: Restrict returns export to store managers in Magento admin\nWhy: The client dropped permissions from release one.\n"
        # the story is no longer in the set as a STORY; it is listed as REMOVED
        gone = re.sub(r"### STORY: Restrict returns export.*?(?=\n### DEP)", "", gone, flags=re.S)
        plan = build_plan(gone, "F", "ref", "SP", m, hashes=h, titles=t, version=2)
        rm = [o for o in plan["ops"] if o["local_id"].startswith("rm:")]
        self.assertEqual([o["local_id"] for o in rm], ["rm:s3"])
        self.assertEqual(rm[0]["on_key"], m["s3"])
        self.assertIn("dropped permissions", rm[0]["body"])
        m["rm:s3"] = "SP-999"
        self.assertEqual([o for o in build_plan(gone, "F", "ref", "SP", m, hashes=h, titles=t, version=2)["ops"] if o["local_id"].startswith("rm:")], [])

    def test_a_removed_marker_that_matches_nothing_is_reported(self):
        _, m, h, t = self.settle(self.TICKETS, version=1)
        plan = build_plan(self.TICKETS + "\n### REMOVED: A story that never existed\nWhy: x\n", "F", "ref", "SP", m, hashes=h, titles=t, version=2)
        self.assertTrue(any("does not match any ticket" in w for w in plan["warnings"]))

    def test_removed_tickets_do_not_break_the_checks(self):
        from dcp.ticketlint import lint_tickets
        res = lint_tickets(self.TICKETS + "\n### REMOVED: Something dropped\nWhy: out of scope\n", RULES, "ready")
        self.assertTrue(res.passed, res.render())

    def test_hashes_and_titles_are_read_back_from_the_log(self):
        d = tempfile.TemporaryDirectory()
        s = open_store(Path(d.name) / "e.jsonl")
        s.append("pulse.mapped", FLOW, "SP", "a", {"local_id": "s1", "key": "SP-2", "hash": "aaaaaaaa", "title": "T1"})
        s.append("pulse.mapped", FLOW, "SP", "a", {"local_id": "chg:s1:bbbbbbbb", "key": "SP-2"})
        evs = s.events()
        self.assertEqual((hashes_from_events(evs)["s1"], titles_from_events(evs)["s1"], mapped_from_events(evs)["s1"]), ("bbbbbbbb", "T1", "SP-2"))
        d.cleanup()


class PlanLoggingTests(FlowBase):
    def test_tickets_created_is_logged_again_after_a_change_request(self):
        self.approved_through_gate2()
        tickets = self.d / "tickets.md"
        tickets.write_text((EX / "tickets_good.md").read_text())
        plan = json.loads(run("tickets-plan", "--project", "SP", *self.base, "--flow", FLOW, "--tickets", str(tickets))[1])
        for i, op in enumerate(plan["ops"], 1):
            run("tickets-record", "--project", "SP", *self.base, "--flow", FLOW, "--local-id", op["local_id"], "--key", f"SP-{i}",
                "--hash", op["hash"], "--title", op["title"])
        run("tickets-plan", "--project", "SP", *self.base, "--flow", FLOW, "--tickets", str(tickets))
        before = len(self.store.events(type="tickets.created"))
        self.cr()
        self.add("pack.drafted", {"version": 2})
        run("tickets-plan", "--project", "SP", *self.base, "--flow", FLOW, "--tickets", str(tickets))
        self.assertEqual(len(self.store.events(type="tickets.created")), before + 1)
        run("tickets-plan", "--project", "SP", *self.base, "--flow", FLOW, "--tickets", str(tickets))
        self.assertEqual(len(self.store.events(type="tickets.created")), before + 1)     # and only once


if __name__ == "__main__":
    unittest.main()
