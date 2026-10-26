import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dcp.cli import main
from dcp.config import load_rules
from dcp.events import open_store
from dcp.scoring import evaluate_store

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules()
T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
DM, TL, PL = "U0EXAMPLE1", "U0EXAMPLE2", "U0PRACTICE9"


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


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.pd = ["--projects-dir", str(d / "projects")]
        self.base = self.pd + ["--data-dir", str(d / "data")]
        self.store = open_store(d / "data" / "SP" / "events.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def cfg(self):
        return json.loads((self.d / "projects" / "SP.json").read_text())

    def people(self, *a):
        return run("people", "SP", *a, *self.pd)


class PeopleTests(Base):
    def test_listing_and_lookup(self):
        code, out = self.people("list")
        self.assertIn("Meera N", out)
        self.assertEqual(self.people("whois", "--slack-id", DM)[1].strip(), "Meera N is the delivery_manager")
        code, out = self.people("whois", "--slack-id", "U0NOBODY99")
        self.assertEqual(code, 1)
        self.assertIn("Copy member ID", out)

    def test_adding_a_practice_lead_and_a_senior_with_a_github_login(self):
        self.assertEqual(self.people("add", "--role", "practice_lead", "--name", "Nina P", "--slack-id", PL)[0], 0)
        self.assertEqual(self.people("add", "--role", "senior", "--name", "Sam R", "--slack-id", "U0SENIOR01", "--github", "samr")[0], 0)
        c = self.cfg()["people"]
        self.assertEqual((c["practice_lead"]["slack_id"], c["senior"]["github"]), (PL, "samr"))
        self.assertEqual(self.people("whois", "--github", "SAMR")[1].strip(), "Sam R is the senior")

    def test_a_bad_id_is_refused_with_how_to_find_the_right_one(self):
        code, out = self.people("add", "--role", "senior", "--name", "X", "--slack-id", "sam.r")
        self.assertEqual(code, 1)
        self.assertIn("Copy member ID", out)
        self.assertNotIn("senior", self.cfg()["people"])

    def test_an_existing_role_is_not_overwritten_by_accident(self):
        code, out = self.people("add", "--role", "tech_lead", "--name", "Other", "--slack-id", "U0OTHER001")
        self.assertEqual(code, 1)
        self.assertIn("--replace", out)
        self.assertEqual(self.people("add", "--role", "tech_lead", "--name", "Other", "--slack-id", "U0OTHER001", "--replace")[0], 0)
        self.assertEqual(self.cfg()["people"]["tech_lead"]["name"], "Other")

    def test_the_delivery_manager_cannot_also_be_the_tech_lead(self):
        code, out = self.people("add", "--role", "tech_lead", "--name", "Meera again", "--slack-id", DM, "--replace")
        self.assertEqual(code, 1)
        self.assertEqual(self.cfg()["people"]["tech_lead"]["slack_id"], TL)

    def test_a_delegate_covers_the_tech_lead_by_default(self):
        self.people("add", "--role", "delegate", "--name", "Stand In", "--slack-id", "U0STANDIN1", "--replace")
        self.assertEqual(self.cfg()["people"]["delegate"]["covers"], ["tech_lead"])

    def test_unknown_ids_everywhere_get_the_same_helpful_message(self):
        for args in (["call-proceed", "--flow", "F", "--reason", "long enough reason", "--by-id", "U0NOBODY99"],
                     ["tier-set", "--flow", "F", "--tier", "T2", "--setter-id", "U0NOBODY99"],
                     ["gate-approve", "--flow", "F", "--gate", "1", "--approver-id", "U0NOBODY99"]):
            code, out = run(args[0], "--project", "SP", *self.base, *args[1:])
            self.assertEqual(code, 1)
            self.assertIn("dcp people", out, args[0])


class DisputeTests(Base):
    def setUp(self):
        super().setUp()
        self.people("add", "--role", "practice_lead", "--name", "Nina P", "--slack-id", PL)
        self.store.append("call.recorded", "SP-REQ-001", "SP", "a", {"title": "bad title"}, ts=T0)

    def c01(self):
        r = evaluate_store(self.store, RULES, flow="SP-REQ-001", now=T0 + timedelta(days=1))[0]
        return r, next(c for c in r.checks if c.id == "C01")

    def dispute(self, who=DM, check="C01", reason="the client renamed the call on purpose"):
        return run("dispute", "--project", "SP", *self.base, "--flow", "SP-REQ-001", "--check", check, "--reason", reason, "--by-id", who)

    def resolve(self, who=PL, outcome="overturned", note="the note was renamed by the client"):
        return run("dispute-resolve", "--project", "SP", *self.base, "--flow", "SP-REQ-001", "--check", "C01",
                   "--outcome", outcome, "--note", note, "--by-id", who)

    def test_an_open_dispute_is_flagged_but_does_not_change_the_score(self):
        r0, c0 = self.c01()
        self.assertEqual(c0.status, "fail")
        self.assertEqual(self.dispute()[0], 0)
        r1, c1 = self.c01()
        self.assertEqual((c1.status, c1.disputed, r1.score), ("fail", "open", r0.score))

    def test_only_a_practice_lead_resolves_so_nobody_judges_their_own_score(self):
        self.dispute()
        code, out = self.resolve(who=DM)
        self.assertEqual(code, 1)
        self.assertIn("practice lead", out)
        self.assertEqual(self.resolve(who=TL)[0], 1)
        self.assertEqual(self.c01()[1].disputed, "open")

    def test_overturned_removes_the_check_from_the_score_and_keeps_the_history(self):
        self.dispute()
        self.assertEqual(self.resolve(outcome="overturned")[0], 0)
        r, c = self.c01()
        self.assertEqual((c.status, c.disputed), ("na", "overturned"))
        self.assertIn("Overturned on dispute", c.detail)
        self.assertIn("Was: fail", c.detail)
        self.assertEqual([e.type for e in self.store.events(flow="SP-REQ-001")][-2:], ["score.disputed", "score.dispute_resolved"])

    def test_upheld_keeps_the_result_and_shows_it_was_reviewed(self):
        self.dispute()
        self.resolve(outcome="upheld", note="the convention was not followed")
        self.assertEqual((self.c01()[1].status, self.c01()[1].disputed), ("fail", "upheld"))

    def test_guards(self):
        self.assertEqual(self.dispute(who="U0NOBODY99")[0], 1)
        self.assertEqual(self.dispute(check="C99")[0], 1)
        self.assertEqual(self.dispute(reason="no")[0], 1)
        self.assertEqual(self.resolve()[0], 1)                      # nothing to resolve yet
        self.dispute()
        self.assertEqual(self.resolve(note="ok")[0], 1)             # needs a real note
        self.assertEqual(run("dispute", "--project", "SP", *self.base, "--flow", "SP-REQ-009", "--check", "C01",
                             "--reason", "a long enough reason", "--by-id", DM)[0], 1)

    def test_a_check_can_be_disputed_again_after_it_was_resolved(self):
        self.dispute()
        self.resolve(outcome="upheld", note="the convention was not followed")
        self.dispute(reason="new evidence: the client confirmed in writing")
        self.assertEqual(self.c01()[1].disputed, "open")


class BreakGlassTests(Base):
    def setUp(self):
        super().setUp()
        self.people("add", "--role", "practice_lead", "--name", "Nina P", "--slack-id", PL)
        self.store.append("call.recorded", "SP-REQ-001", "SP", "a", {"title": "x :: y :: SP"}, ts=T0)

    def use(self, who=DM, reason="production checkout is down for every customer"):
        return run("breakglass", "--project", "SP", *self.base, "--flow", "SP-REQ-001", "--reason", reason, "--by-id", who)

    def review(self, who=PL):
        return run("breakglass-review", "--project", "SP", *self.base, "--flow", "SP-REQ-001", "--by-id", who)

    def c19(self, days):
        r = evaluate_store(self.store, RULES, flow="SP-REQ-001", now=T0 + timedelta(days=days))[0]
        return next(c for c in r.checks if c.id == "C19")

    def test_use_is_recorded_and_pending_then_fails_if_nobody_reviews_it(self):
        self.assertEqual(self.use()[0], 0)
        used = self.store.events(type="breakglass.used")[0]
        self.assertEqual(used.actor, "Meera N")
        self.assertEqual(self.c19(60).status, "fail")

    def test_a_review_by_someone_else_closes_it(self):
        self.use()
        self.assertEqual(self.review(PL)[0], 0)
        self.assertEqual(self.c19(60).status, "pass")

    def test_you_cannot_review_your_own_emergency_and_ordinary_people_cannot_review(self):
        self.use(who=DM)
        code, out = self.review(DM)
        self.assertEqual(code, 1)
        self.assertIn("cannot be the one who reviews", out)
        self.people("add", "--role", "senior", "--name", "Sam R", "--slack-id", "U0SENIOR01")
        self.assertEqual(self.review("U0SENIOR01")[0], 1)
        self.assertEqual(self.review("U0NOBODY99")[0], 1)

    def test_guards(self):
        self.assertEqual(self.use(reason="outage")[0], 1)
        self.assertEqual(self.use(who="U0NOBODY99")[0], 1)
        self.assertEqual(self.review()[0], 1)                       # nothing to review yet
        self.assertEqual(run("breakglass", "--project", "SP", *self.base, "--flow", "SP-REQ-099", "--reason", "a long enough reason", "--by-id", DM)[0], 1)


class ImportTests(Base):
    def setUp(self):
        super().setUp()
        self.store.append("call.recorded", "SP-REQ-001", "SP", "a", {"title": "x :: y :: SP"}, ts=T0)
        self.store.append("call.recorded", "SP-REQ-002", "SP", "a", {"title": "x :: z :: SP"}, ts=T0)

    def csv(self, name, text):
        p = self.d / name
        p.write_text(text)
        return str(p)

    def imp(self, kind, text):
        return run(f"import-{kind}", "--project", "SP", *self.base, "--csv", self.csv(f"{kind}.csv", text))

    def metrics(self, flow="SP-REQ-001"):
        return evaluate_store(self.store, RULES, flow=flow, now=T0 + timedelta(days=1))[0].metrics

    def test_hours_import_feeds_the_overrun_metric(self):
        code, out = self.imp("hours", "flow,estimate_hours,actual_hours\nSP-REQ-001,80,104\nSP-REQ-002,40,38\n")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.metrics()["overrun_pct"], 30.0)
        self.assertEqual(self.metrics("SP-REQ-002")["overrun_pct"], -5.0)

    def test_importing_the_same_file_twice_adds_nothing(self):
        text = "flow,estimate_hours,actual_hours\nSP-REQ-001,80,104\n"
        self.imp("hours", text)
        code, out = self.imp("hours", text)
        self.assertIn("skipped 1", out)
        self.assertEqual(len(self.store.events(type="hours.recorded")), 1)

    def test_bad_rows_are_reported_and_good_rows_still_go_in(self):
        code, out = self.imp("hours", "flow,estimate_hours,actual_hours\nSP-REQ-001,80,104\nSP-REQ-077,10,10\nSP-REQ-002,0,5\nSP-REQ-002,abc,5\n")
        self.assertEqual(code, 1)
        self.assertIn("imported 1", out)
        self.assertEqual(sum(1 for line in out.splitlines() if line.strip().startswith("line ")), 3)
        self.assertEqual(len(self.store.events(type="hours.recorded")), 1)

    def test_a_missing_column_is_reported_not_crashed(self):
        code, out = self.imp("hours", "flow,estimate\nSP-REQ-001,80\n")
        self.assertEqual(code, 1)
        self.assertIn("line 2", out)

    def test_rework_import_counts_and_dedupes_by_row(self):
        text = ("flow,type,detail,date\nSP-REQ-001,ticket.reopened,SP-12,2026-09-03\nSP-REQ-001,defect.found,uat,2026-09-04\n"
                "SP-REQ-001,change_request.raised,client added a filter,2026-09-05\nSP-REQ-001,assumption_gap.found,prod,2026-09-06\n"
                "SP-REQ-001,defect.found,dev,2026-09-06\n")
        self.assertEqual(self.imp("rework", text)[0], 0)
        m = self.metrics()
        self.assertEqual(m["rework_events"], 4)          # the 'dev' defect is not late rework
        self.assertIn("skipped 5", self.imp("rework", text)[1])
        self.assertEqual(self.metrics()["rework_events"], 4)

    def test_rework_validation(self):
        code, out = self.imp("rework", "flow,type,detail\nSP-REQ-001,defect.found,sometime\nSP-REQ-001,wrong.type,x\nSP-REQ-001,ticket.reopened,\nSP-REQ-009,ticket.reopened,SP-1\n")
        self.assertEqual(code, 1)
        self.assertEqual(sum(1 for line in out.splitlines() if line.strip().startswith("line ")), 4)


if __name__ == "__main__":
    unittest.main()
