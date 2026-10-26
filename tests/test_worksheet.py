import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from dcp.cli import main
from dcp.config import load_rules
from dcp.events import open_store
from dcp.models import ACTION_KIND
from dcp.project import load_project
from dcp.worksheet import (build_worksheet, email_questions, msg_followup, msg_gate1, msg_gate2, pack_summary, render)

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
RULES = load_rules()
PACK = (EX / "pack_good.md").read_text()
TICKETS = (EX / "tickets_good.md").read_text()
CALL = (EX / "transcript.txt").read_text()


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


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_project(ROOT / "projects" / "SP.json")

    def test_pack_summary_reads_the_pack(self):
        p = pack_summary(PACK)
        self.assertEqual((p["title"], len(p["scope"]), p["criteria"], len(p["questions"])), ("Returns export", 3, 4, 3))
        self.assertIn("cannot find last month", p["summary"])
        self.assertEqual(p["questions"][0][1], "Priya (client IT)")

    def test_gate1_message_carries_the_client_s_words_scope_and_open_questions(self):
        m = msg_gate1("SP-REQ-001", PACK, 1, self.cfg)
        for needle in ("Gate 1", "pack v1", "cannot find last month", "3 things in scope", "4 acceptance criteria", "Still to confirm", "Meera N"):
            self.assertIn(needle, m)

    def test_gate2_message_lists_stories_and_says_sizing_is_separate_and_optional(self):
        m = msg_gate2("SP-REQ-001", TICKETS, self.cfg, ["delivery_manager"])
        self.assertIn("S1. Add returns export button", m)
        self.assertIn("S3. Restrict returns export", m)
        self.assertIn("optional and separate", m)
        self.assertNotIn("and Arjun K: validate", m)
        self.assertIn("Meera N and Arjun K", msg_gate2("F", TICKETS, self.cfg, ["delivery_manager", "tech_lead"]))

    def test_followup_message_names_what_is_missing_and_how_to_join_the_next_call(self):
        m = msg_followup("SP-REQ-001", ["what_done_looks_like", "date_and_reason"], 5, self.cfg)
        self.assertIn("what done looks like", m)
        self.assertIn("Flow: SP-REQ-001", m)

    def test_the_client_email_is_numbered_and_asks_for_numbered_replies(self):
        m = email_questions("F", PACK)
        self.assertIn("1. Which SFTP target", m)
        self.assertIn("3. Does Rahul approve", m)
        self.assertIn("reply with a number", m)
        self.assertIn("no open questions", email_questions("F", PACK.split("## 7.")[0] + "## 7. Open questions\nNone\n"))


class EveryStepHasManualInstructions(unittest.TestCase):
    def test_no_step_falls_through_to_the_placeholder(self):
        d = Path(tempfile.mkdtemp())
        cfg = load_project(ROOT / "projects" / "SP.json")
        fd = d / "SP" / "SP-REQ-001"
        fd.mkdir(parents=True)
        (fd / "pack_v1.md").write_text(PACK)
        (fd / "tickets.md").write_text(TICKETS)
        for action in ACTION_KIND:
            order = {"flow": "SP-REQ-001", "action": action, "why": "w", "kind": "task",
                     "inputs": {"gate": 1, "version": 1, "check": "gate1", "owner": "delivery_manager", "to": "owner",
                                "reason": "pack_lint", "missing": ["boundaries"], "branch": "feature/SP-2-x", "tier": "T2",
                                "pack_version": 2, "count": 3}}
            items = build_worksheet(cfg, [order], [], d, "data/SP/events.jsonl", RULES)
            self.assertEqual(len(items), 1, action)
            self.assertNotIn("No manual steps are written", " ".join(items[0]["steps"]), f"{action} has no manual instructions")
            self.assertTrue(items[0]["steps"], action)
        shutil.rmtree(d)

    def test_a_step_that_needs_a_record_gives_the_command_that_records_it(self):
        d = Path(tempfile.mkdtemp())
        cfg = load_project(ROOT / "projects" / "SP.json")
        (d / "SP" / "SP-REQ-001").mkdir(parents=True)
        needs_record = ["open_gate", "nudge", "escalate", "request_followup", "notify_change", "launch_build", "prepare_questions",
                        "draft_pack", "draft_tickets", "revise_pack", "create_support_ticket", "create_pulse_tickets"]
        for action in needs_record:
            it = build_worksheet(cfg, [{"flow": "SP-REQ-001", "action": action, "why": "", "inputs": {"gate": 2}}], [], d, "s.jsonl", RULES)[0]
            self.assertTrue(any("python3 -m dcp" in c for c in it["commands"]), action)
        shutil.rmtree(d)


class EndToEndManualRun(unittest.TestCase):
    """A project with no connections at all, driven only by the worksheet and the commands it prints."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.base = ["--projects-dir", str(d / "projects"), "--data-dir", str(d / "data")]
        (d / "data" / "SP" / "inbox").mkdir(parents=True)
        self.store = open_store(d / "data" / "SP" / "events.jsonl")
        for st in ("pulse_sweep", "governance", "slack"):
            self.store.append("poll.ran", "_poll", "SP", "r", {"stage": st})

    def tearDown(self):
        self.dir.cleanup()

    def sheet(self):
        return json.loads(run("worksheet", "--project", "SP", *self.base, "--json")[1])

    def test_the_sheet_walks_a_flow_from_paste_to_the_first_gate(self):
        (self.d / "data" / "SP" / "inbox" / "call.txt").write_text("Title: SiteOne :: returns export :: SP\n" + CALL)
        run("tick", "--project", "SP", *self.base)                                  # ingests and assesses, no tokens
        items = self.sheet()["items"]
        draft = [i for i in items if i["order"].endswith("draft_pack")][0]
        self.assertIn("stage2_requirement_pack.md", " ".join(draft["steps"]))
        self.assertTrue(any("pack.drafted" in c for c in draft["commands"]))
        # the person drafts the pack by hand, saves it and records it
        (self.d / "data" / "SP" / "SP-REQ-001" / "pack_v1.md").write_text(PACK)
        self.store.append("pack.drafted", "SP-REQ-001", "SP", "me", {"version": 1})
        run("tick", "--project", "SP", *self.base)                                  # the check runs by itself
        titles = [i["what"] for i in self.sheet()["items"] if i["order"].startswith("SP-REQ-001")]
        self.assertIn("Post a gate prompt", titles)
        gate = [i for i in self.sheet()["items"] if i["order"].endswith("open_gate")][0]
        self.assertIn("Gate 1", gate["message"])
        self.assertIn("cannot find last month", gate["message"])
        self.assertTrue(any("gate-open" in c for c in gate["commands"]))
        q = [i for i in self.sheet()["items"] if i["order"].endswith("prepare_questions")][0]
        self.assertIn("1. Which SFTP target", q["message"])

    def test_text_output_is_readable_and_lists_who_is_being_waited_on(self):
        (self.d / "data" / "SP" / "inbox" / "call.txt").write_text("Title: SiteOne :: returns export :: SP\n" + CALL.replace("deadline", "wish"))
        run("tick", "--project", "SP", *self.base)
        code, out = run("worksheet", "--project", "SP", *self.base)
        self.assertEqual(code, 0)
        self.assertIn("who:", out)
        self.assertIn("then run:", out)
        code, out = run("worksheet", "--project", "SP", *self.base, "--flow", "SP-REQ-999")
        self.assertIn("Nothing to do", out)


if __name__ == "__main__":
    unittest.main()
