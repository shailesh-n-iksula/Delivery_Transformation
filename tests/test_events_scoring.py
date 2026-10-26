import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dcp.config import load_rules
from dcp.events import EventError, EventStore
from dcp.scoring import FAIL, NA, PASS, PENDING, correlate, evaluate_flow, evaluate_store

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
RULES = load_rules()


def status(report, cid):
    return next(c for c in report.checks if c.id == cid).status


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.dir.name) / "e.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def add(self, type_, data, dt=T0, flow="F1", actor="a", track="build"):
        return self.store.append(type_, flow, "SP", actor, data, ts=dt, track=track)

    def test_append_validates_type_and_required_keys(self):
        with self.assertRaises(EventError):
            self.add("nonsense", {})
        with self.assertRaises(EventError):
            self.add("call.recorded", {})
        with self.assertRaises(EventError):
            self.store.append("call.recorded", "", "SP", "a", {"title": "x"})

    def test_intact_chain_verifies(self):
        self.add("call.recorded", {"title": "x"})
        self.add("pack.drafted", {"version": 1}, T0 + timedelta(minutes=5))
        self.assertEqual(self.store.verify(), [])

    def test_editing_an_event_is_detected(self):
        self.add("call.recorded", {"title": "x"})
        self.add("pack.drafted", {"version": 1}, T0 + timedelta(minutes=5))
        p = self.store.path
        p.write_text(p.read_text().replace('"version": 1', '"version": 9'), encoding="utf-8")
        self.assertTrue(self.store.verify())

    def test_deleting_an_event_is_detected(self):
        for i in range(3):
            self.add("pack.drafted", {"version": i}, T0 + timedelta(minutes=i))
        lines = self.store.path.read_text().splitlines()
        self.store.path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
        self.assertTrue(self.store.verify())

    def test_events_returned_in_time_order_and_filterable(self):
        self.add("pack.drafted", {"version": 1}, T0 + timedelta(hours=1))
        self.add("call.recorded", {"title": "x"}, T0)
        self.add("call.recorded", {"title": "y"}, T0, flow="F2")
        self.assertEqual([e.type for e in self.store.events(flow="F1")], ["call.recorded", "pack.drafted"])
        self.assertEqual(self.store.flows(), ["F1", "F2"])

    def test_naive_timestamps_are_treated_as_utc(self):
        e = self.add("call.recorded", {"title": "x"}, dt="2026-09-01T10:00:00")
        self.assertEqual(e.ts.tzinfo, timezone.utc)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.dir.name) / "e.jsonl")
        self.now = T0 + timedelta(days=30)

    def tearDown(self):
        self.dir.cleanup()

    def add(self, type_, data, minutes=0, flow="F1", track="build"):
        self.store.append(type_, flow, "SP", "a", data, ts=T0 + timedelta(minutes=minutes), track=track)

    def report(self, flow="F1", now=None):
        return evaluate_store(self.store, RULES, flow=flow, now=now or self.now)[0]

    def test_empty_ledger_scores_nothing_not_zero(self):
        self.add("call.recorded", {"title": "ACME :: topic :: SP"})
        r = self.report(now=T0 + timedelta(minutes=5))
        self.assertEqual(status(r, "C01"), PASS)
        self.assertEqual(status(r, "C02"), PENDING)
        self.assertEqual(r.score, 100.0)

    def test_pack_overdue_fails_but_not_before_deadline(self):
        self.add("call.recorded", {"title": "bad title"})
        self.assertEqual(status(self.report(now=T0 + timedelta(minutes=30)), "C02"), PENDING)
        self.assertEqual(status(self.report(now=T0 + timedelta(minutes=90)), "C02"), FAIL)

    def test_naming_convention_fails_on_free_text(self):
        self.add("call.recorded", {"title": "returns call"})
        self.assertEqual(status(self.report(), "C01"), FAIL)

    def test_rubber_stamp_gate_fails(self):
        self.add("gate.opened", {"gate": 1}, 0)
        self.store.append("gate.approved", "F1", "SP", "dm", {"gate": 1, "approver": "dm", "approver_role": "delivery_manager"},
                          ts=T0 + timedelta(seconds=10))
        self.assertEqual(status(self.report(), "C05"), FAIL)

    def test_gate_two_by_wrong_role_fails(self):
        self.add("gate.approved", {"gate": 2, "approver": "dev", "approver_role": "developer"})
        self.assertEqual(status(self.report(), "C09"), FAIL)

    def test_gate_two_needs_the_delivery_manager_and_tech_lead_only_when_required(self):
        self.add("gate.opened", {"gate": 2, "required_roles": ["delivery_manager"]}, 0)
        self.add("gate.approved", {"gate": 2, "approver": "tl", "approver_role": "tech_lead"}, 5)
        self.assertEqual(status(self.report(), "C09"), PENDING)  # tech lead alone does not validate
        self.add("gate.approved", {"gate": 2, "approver": "dm", "approver_role": "delivery_manager"}, 6)
        self.assertEqual(status(self.report(), "C09"), PASS)

    def test_gate_two_waits_for_tech_lead_when_the_project_requires_it(self):
        self.add("gate.opened", {"gate": 2, "required_roles": ["delivery_manager", "tech_lead"]}, 0)
        self.add("gate.approved", {"gate": 2, "approver": "dm", "approver_role": "delivery_manager"}, 5)
        r = self.report()
        self.assertEqual(status(r, "C09"), PENDING)
        self.assertIn("tech_lead", next(c for c in r.checks if c.id == "C09").detail)
        self.add("gate.approved", {"gate": 2, "approver": "tl", "approver_role": "tech_lead"}, 6)
        self.assertEqual(status(self.report(), "C09"), PASS)

    def test_no_open_questions_makes_the_send_check_na(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("pack.linted", {"version": 1, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0}, 10)
        self.assertEqual(status(self.report(), "C04"), NA)

    def test_parakh_verdict_must_match_latest_commit(self):
        self.add("pr.opened", {"pr": 7, "author": "dev", "head_sha": "aaa", "ticket": "SP-1", "ai_declared": True, "tier": "T2"})
        self.add("parakh.verdict", {"pr": 7, "head_sha": "aaa", "gate": "pass"}, 5)
        self.assertEqual(status(self.report(), "C13"), PASS)
        self.add("pr.pushed", {"pr": 7, "head_sha": "bbb"}, 30)
        r = self.report()
        self.assertEqual(status(r, "C13"), FAIL)
        self.assertIn("latest commit", next(c for c in r.checks if c.id == "C13").detail)

    def test_parakh_failing_gate_fails(self):
        self.add("pr.opened", {"pr": 7, "author": "dev", "head_sha": "aaa", "tier": "T2"})
        self.add("parakh.verdict", {"pr": 7, "head_sha": "aaa", "gate": "fail"}, 5)
        self.assertEqual(status(self.report(), "C13"), FAIL)

    def test_t3_makes_parakh_optional(self):
        self.add("tier.set", {"tier": "T3", "actor_role": "tech_lead"})
        self.add("pr.opened", {"pr": 7, "author": "dev", "head_sha": "aaa"}, 5)
        self.assertEqual(status(self.report(), "C13"), NA)

    def test_maker_cannot_check_own_work(self):
        self.add("pr.opened", {"pr": 7, "author": "dev1", "head_sha": "a"})
        self.add("pr.merged", {"pr": 7, "merger": "dev1", "merger_role": "tech_lead", "approvers": ["dev1"]}, 10)
        self.assertEqual(status(self.report(), "C14"), FAIL)

    def test_t1_needs_senior_and_evidence(self):
        self.add("tier.set", {"tier": "T1", "actor_role": "tech_lead"})
        self.add("pr.opened", {"pr": 7, "author": "dev1", "head_sha": "a", "ticket": "SP-1"}, 5)
        self.add("pr.merged", {"pr": 7, "merger": "tl", "merger_role": "tech_lead", "approvers": ["dev2"]}, 10)
        r = self.report()
        self.assertEqual(status(r, "C14"), FAIL)
        self.assertEqual(status(r, "C17"), FAIL)

    def test_tier_set_after_work_started_fails(self):
        self.add("build.started", {"ticket": "SP-1"}, 0)
        self.add("tier.set", {"tier": "T2", "actor_role": "tech_lead"}, 60)
        self.assertEqual(status(self.report(), "C16"), FAIL)

    def test_tier_set_by_non_lead_fails(self):
        self.add("tier.set", {"tier": "T2", "actor_role": "developer"}, 0)
        self.add("build.started", {"ticket": "SP-1"}, 60)
        self.assertEqual(status(self.report(), "C16"), FAIL)

    def test_assumptions_note_grace_period(self):
        self.add("build.started", {"ticket": "SP-1"}, 0)
        self.assertEqual(status(self.report(now=T0 + timedelta(days=1)), "C10"), PENDING)
        self.assertEqual(status(self.report(now=T0 + timedelta(days=5)), "C10"), FAIL)
        self.add("assumptions.received", {"ticket": "SP-1"}, 600)
        self.assertEqual(status(self.report(now=T0 + timedelta(days=5)), "C10"), PASS)

    def test_breakglass_must_be_reviewed(self):
        self.add("breakglass.used", {"reason": "prod down"})
        self.assertEqual(status(self.report(now=T0 + timedelta(hours=2)), "C19"), PENDING)
        self.assertEqual(status(self.report(now=T0 + timedelta(days=3)), "C19"), FAIL)
        self.add("breakglass.reviewed", {"reviewer": "head"}, 60 * 5)
        self.assertEqual(status(self.report(now=T0 + timedelta(days=3)), "C19"), PASS)

    def test_support_track_skips_build_only_checks(self):
        self.add("ticket.created", {"ticket": "ML-1"}, track="support")
        r = self.report()
        self.assertEqual(r.track, "support")
        self.assertEqual(status(r, "C02"), NA)
        self.assertEqual(status(r, "C09"), NA)

    def test_disabled_check_is_omitted_and_weights_drive_score(self):
        rules = json.loads(json.dumps(RULES))
        rules["compliance"]["checks"]["C01"]["enabled"] = False
        self.add("call.recorded", {"title": "bad"})
        evs = self.store.events(flow="F1")
        r = evaluate_flow(evs, rules, self.now)
        self.assertNotIn("C01", [c.id for c in r.checks])

    def test_score_is_weighted(self):
        self.add("call.recorded", {"title": "ACME :: t :: SP"})
        self.add("pack.drafted", {"version": 1}, 300)
        r = self.report()
        self.assertEqual(status(r, "C01"), PASS)
        self.assertEqual(status(r, "C02"), FAIL)
        self.assertAlmostEqual(r.score, round(1 / (1 + 2) * 100, 1))

    def test_metrics_and_rework_counted(self):
        self.add("call.recorded", {"title": "ACME :: t :: SP"})
        self.add("pack.drafted", {"version": 1}, 14)
        self.add("hours.recorded", {"estimate_hours": 100, "actual_hours": 130}, 2000)
        self.add("defect.found", {"phase": "uat"}, 2100)
        self.add("defect.found", {"phase": "dev"}, 2100)
        self.add("change_request.raised", {"reason": "x"}, 2100)
        m = self.report().metrics
        self.assertEqual(m["call_to_pack_min"], 14.0)
        self.assertEqual(m["overrun_pct"], 30.0)
        self.assertEqual(m["rework_events"], 2)

    def test_every_score_traces_to_event_ids(self):
        e = self.store.append("call.recorded", "F1", "SP", "a", {"title": "x"}, ts=T0)
        c = next(c for c in self.report().checks if c.id == "C01")
        self.assertEqual(c.evidence, [e.id])


class CorrelationTests(unittest.TestCase):
    def test_too_few_flows_refuses_to_conclude(self):
        self.assertIn("Too few", correlate([], RULES, "rework_events")["note"])

    def test_negative_relationship_detected_and_caveated(self):
        from dcp.scoring import FlowReport
        reps = [FlowReport(f"F{i}", "SP", "build", s, [], {"rework_events": r})
                for i, (s, r) in enumerate([(95, 0), (90, 1), (85, 1), (60, 4), (40, 6), (30, 8)])]
        out = correlate(reps, RULES, "rework_events")
        self.assertLess(out["spearman"], -0.9)
        self.assertLess(out["high_compliance_avg"], out["low_compliance_avg"])
        self.assertIn("Indicative only", out["note"])


if __name__ == "__main__":
    unittest.main()
