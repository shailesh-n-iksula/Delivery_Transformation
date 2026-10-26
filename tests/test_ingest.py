import json
import shutil
import tempfile
import unittest
from pathlib import Path

from dcp.cli import main
from dcp.config import load_rules
from dcp.events import EventStore
from dcp.ingest import content_ref, looks_like_summary, source_problem, split_paste
from dcp.project import load_project, make_project, validate_project, with_defaults
from dcp.tick import tick
import contextlib
import io

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
RULES = load_rules()

CALL = (EX / "transcript.txt").read_text()
GRANOLA = ("Meeting Title: SiteOne :: returns export :: SP\nDate: Oct 16\nMeeting participants: Meera, Priya\n\n"
           "Transcript:\n" + CALL.replace("\n", " "))
SUMMARY = ("# Weekly Planning Notes\n" + "\n".join(f"- point {i} about what the team talked about with the client today and agreed" for i in range(16))
           + "\n# Next Steps\n- Start sharing emails\n- Adapt case studies\n")


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


class ParsingTests(unittest.TestCase):
    def test_granola_copy_header_is_read_and_stripped(self):
        title, body, meta = split_paste(GRANOLA)
        self.assertEqual(title, "SiteOne :: returns export :: SP")
        self.assertEqual(meta["date"], "Oct 16")
        self.assertIn("Meera, Priya", meta["participants"])
        self.assertTrue(body.startswith("Meera (Iksula)"))
        self.assertNotIn("Meeting Title", body)

    def test_a_simple_title_line_works_and_a_bare_paste_has_no_title(self):
        title, body, _ = split_paste("Title: ACME :: x :: SP\n" + CALL)
        self.assertEqual(title, "ACME :: x :: SP")
        self.assertTrue(body.startswith("Meera"))
        self.assertEqual(split_paste(CALL)[0], None)

    def test_summary_detection_does_not_flag_real_calls(self):
        self.assertIsNone(looks_like_summary(CALL))
        self.assertIsNone(looks_like_summary(CALL.replace("\n", " ")))           # one long paragraph, as Granola copies it
        self.assertIsNone(looks_like_summary("Me: " + "and then we talked about it and about it again " * 40))
        self.assertTrue(looks_like_summary(SUMMARY))

    def test_source_problem_covers_short_and_summary(self):
        self.assertIn("too short", source_problem("Me: tiny"))
        self.assertIn("AI summary", source_problem(SUMMARY))
        self.assertIsNone(source_problem(CALL))

    def test_the_same_call_gets_the_same_reference_whatever_the_formatting(self):
        self.assertEqual(content_ref(CALL), content_ref("  " + CALL.upper().replace("\n", "  \n ")))


class CoverageRegressionTests(unittest.TestCase):
    """Phrases from a real, machine-transcribed call that used to count as 'covered' and hid real gaps."""

    def covered(self, text):
        from dcp.packlint import assess_call
        return assess_call(text, RULES)["covered"]

    def test_everyday_speech_does_not_count_as_covering_a_topic(self):
        for phrase in ("you can't like, make it completely finished",
                       "reports launch reports basically",
                       "The campaigns will automatically change for that time frame",
                       "If the problem is of a certain kind, then the data has to be tidy",
                       "Photos need to be resized into small versions and it should not feel crowded"):
            self.assertEqual(self.covered(phrase), [], phrase)

    def test_real_statements_still_count(self):
        self.assertEqual(self.covered("A store manager cannot find last month's returns"), ["job_to_be_done"])
        self.assertEqual(self.covered("We must not change the existing returns grid"), ["boundaries"])
        self.assertEqual(self.covered("The audit starts on the first of December"), ["date_and_reason"])
        self.assertEqual(self.covered("Today they open the admin and export it"), ["what_happens_today"])

    def test_a_pattern_entry_needs_the_right_subject(self):
        self.assertEqual(self.covered("we cannot see which orders failed"), ["job_to_be_done"])
        self.assertEqual(self.covered("the printer cannot do that"), [])

    def test_whole_words_only(self):
        self.assertEqual(self.covered("campaign"), ["date_and_reason"])
        self.assertEqual(self.covered("campaigns"), [])


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.base = ["--projects-dir", str(d / "projects"), "--data-dir", str(d / "data")]
        self.inbox = d / "data" / "SP" / "inbox"
        self.inbox.mkdir(parents=True)
        self.store = EventStore(d / "data" / "SP" / "events.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def ingest(self):
        return json.loads(run("ingest", "--project", "SP", *self.base, "--json")[1])

    def test_a_pasted_transcript_becomes_a_flow_and_is_filed_away(self):
        (self.inbox / "call.txt").write_text(GRANOLA)
        r = self.ingest()
        self.assertEqual([i["flow"] for i in r["ingested"]], ["SP-REQ-001"])
        saved = (self.d / "data" / "SP" / "SP-REQ-001" / "transcript.txt").read_text()
        self.assertTrue(saved.startswith("Meera (Iksula)"))
        self.assertEqual([p.name for p in (self.inbox / "processed").iterdir()], ["SP-REQ-001__call.txt"])
        self.assertEqual(list(self.inbox.glob("*.txt")), [])
        ev = [e for e in self.store.events() if e.type == "call.recorded"][0]
        self.assertEqual((ev.data["title"], ev.data["date"]), ("SiteOne :: returns export :: SP", "Oct 16"))
        self.assertTrue(ev.data["source_ref"].startswith("paste:"))

    def test_pasting_the_same_call_twice_does_not_draft_it_twice(self):
        (self.inbox / "a.txt").write_text(GRANOLA)
        self.ingest()
        (self.inbox / "b.txt").write_text(GRANOLA.replace("Oct 16", "Oct 17"))
        r = self.ingest()
        self.assertEqual((r["ingested"], [d["flow"] for d in r["duplicates"]]), ([], ["SP-REQ-001"]))
        self.assertEqual(len(self.store.flows("SP")), 1)

    def test_the_file_name_is_the_title_when_there_is_no_header(self):
        (self.inbox / "SiteOne :: returns :: SP.txt").write_text(CALL)
        self.assertEqual(self.ingest()["ingested"][0]["title"], "SiteOne :: returns :: SP")

    def test_a_title_that_does_not_follow_the_convention_is_flagged(self):
        (self.inbox / "returns export call.txt").write_text(CALL)
        r = self.ingest()
        self.assertEqual(r["needs_rename"][0]["flow"], "SP-REQ-001")

    def test_a_call_tagged_for_another_project_is_refused_and_stays_put(self):
        (self.inbox / "x.txt").write_text(GRANOLA.replace(":: SP", ":: ML"))
        r = self.ingest()
        self.assertEqual((r["ingested"], len(r["rejected"])), ([], 1))
        self.assertIn("ML", r["rejected"][0]["reason"])
        self.assertTrue((self.inbox / "x.txt").exists())
        self.assertEqual(self.store.flows("SP"), [])

    def test_a_summary_is_refused_and_stays_put_with_the_reason(self):
        (self.inbox / "notes.txt").write_text(SUMMARY)
        r = self.ingest()
        self.assertEqual(r["ingested"], [])
        self.assertIn("AI summary", r["rejected"][0]["reason"])
        self.assertTrue((self.inbox / "notes.txt").exists())
        self.assertEqual(run("ingest", "--project", "SP", *self.base)[0], 1)   # non-zero so a script notices

    def test_a_too_short_paste_is_refused(self):
        (self.inbox / "tiny.txt").write_text("Me: a quick thought")
        self.assertIn("too short", self.ingest()["rejected"][0]["reason"])

    def test_files_starting_with_an_underscore_or_dot_are_ignored(self):
        (self.inbox / "_HOW_TO_USE.md").write_text("Paste a transcript here.")
        (self.inbox / ".hidden.txt").write_text("x")
        self.assertEqual(self.ingest()["rejected"], [])

    def test_non_text_files_and_the_processed_folder_are_ignored(self):
        (self.inbox / "photo.png").write_bytes(b"x")
        (self.inbox / "processed").mkdir()
        (self.inbox / "processed" / "old.txt").write_text(GRANOLA)
        r = self.ingest()
        self.assertEqual((r["ingested"], r["rejected"]), ([], []))

    def test_the_tick_ingests_assesses_and_stops_before_any_drafting_if_the_call_is_thin(self):
        for st in ("pulse_sweep", "governance"):
            self.store.append("poll.ran", "_poll", "SP", "r", {"stage": st})
        (self.inbox / "good.txt").write_text(GRANOLA)
        cfg = load_project(self.d / "projects" / "SP.json")
        r = tick(cfg, RULES, self.d / "data", self.d / "data" / "SP" / "events.jsonl", self.d / "projects")
        self.assertEqual(r["ingest"]["ingested"][0]["flow"], "SP-REQ-001")
        self.assertIn("SP-REQ-001:assess_call", r["ran_without_ai"])
        self.assertEqual([o["action"] for s in r["sessions"] for o in s["orders"]], ["draft_pack"])  # ready: drafting is next


class PasteModeConfigTests(unittest.TestCase):
    def make(self, **kw):
        return make_project("SP", "build", "https://pulse.iksula.com/projects/SP/board", "#sp", git_repo="iksula/x",
                            delivery_manager="M:U0EXAMPLE1", tech_lead="A:U0EXAMPLE2", **kw)

    def test_paste_is_the_default_and_needs_no_granola_folder(self):
        cfg = self.make()
        self.assertEqual(cfg["granola"]["source"], "paste")
        self.assertEqual(validate_project(cfg), [])

    def test_connector_mode_needs_a_folder(self):
        self.assertTrue(any("granola.folder" in p for p in validate_project(self.make(granola_source="connector"))))
        self.assertEqual(validate_project(self.make(granola_source="connector", granola_folder="SiteOne")), [])

    def test_unknown_source_is_refused_and_old_files_default_to_paste(self):
        cfg = self.make()
        cfg["granola"]["source"] = "scrape"
        self.assertTrue(any("must be 'paste' or 'connector'" in p for p in validate_project(cfg)))
        self.assertEqual(with_defaults({"key": "SP", "track": "build"})["granola"]["source"], "paste")


if __name__ == "__main__":
    unittest.main()
