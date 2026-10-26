import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dcp.config import load_rules
from dcp.dashboard import render_dashboard
from dcp.events import open_store
from dcp.project import load_project
from dcp.server import Context, handle

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules()
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=5)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.cfg = load_project(d / "projects" / "SP.json")
        self.store = open_store(d / "data" / "SP" / "events.jsonl")
        self.n = 0

    def tearDown(self):
        self.dir.cleanup()

    def add(self, type_, data, flow="SP-REQ-001"):
        self.n += 1
        return self.store.append(type_, flow, "SP", "a", data, ts=T0 + timedelta(minutes=self.n))

    def page(self, label=""):
        return render_dashboard(self.cfg, self.store, RULES, NOW, label)

    def test_an_empty_project_says_so_and_still_renders(self):
        p = self.page()
        self.assertIn("No flows yet", p)
        self.assertIn("intact", p)
        self.assertIn("Nobody is being waited on", p)

    def test_flows_stage_score_and_who_is_being_waited_on(self):
        self.add("call.recorded", {"title": "ACME :: t :: SP"})
        self.add("call.assessed", {"covered": ["a"] * 6, "missing": [], "ready": True, "needed": 5})
        self.add("pack.drafted", {"version": 1})
        self.add("pack.linted", {"version": 1, "passed": True, "blocks": 0, "warns": 0, "open_questions": 0})
        self.add("gate.opened", {"gate": 1, "pack_version": 1})
        p = self.page()
        self.assertIn("SP-REQ-001", p)
        self.assertIn("pack checked", p)
        self.assertIn("delivery_manager", p)                     # who is being waited on, by role
        self.assertIn("gate1", p)
        self.assertRegex(p, r"C0[1-9]")

    def test_content_from_the_log_is_escaped(self):
        self.add("call.recorded", {"title": "<script>alert(1)</script> :: x :: SP"})
        self.add("score.disputed", {"check": "C01", "reason": "<img src=x onerror=alert(2)>"})
        p = self.page()
        self.assertNotIn("<script>alert", p)
        self.assertNotIn("<img src=x", p)
        self.assertIn("&lt;img src=x", p)

    def test_the_page_is_self_contained(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        p = self.page()
        self.assertNotRegex(p, r"(src|href)\s*=\s*['\"]?https?://")
        self.assertNotIn("<script", p.lower())
        self.assertNotIn("@import", p)
        self.assertIn('name="viewport"', p)
        self.assertIn("prefers-color-scheme:dark", p)

    def test_a_tampered_log_is_announced_at_the_top(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        self.add("pack.drafted", {"version": 1})
        path = self.d / "data" / "SP" / "events.jsonl"
        path.write_text(path.read_text().replace('"version": 1', '"version": 2'))
        p = self.page()
        self.assertIn("failed its integrity check", p)
        self.assertLess(p.index("failed its integrity check"), p.index("<h2>"))

    def test_a_label_is_shown_for_demo_data(self):
        self.assertIn("SYNTHETIC DEMO DATA", self.page("SYNTHETIC DEMO DATA: invented"))
        self.assertNotIn("SYNTHETIC", self.page())

    def test_disputes_hours_tokens_and_rework_appear_when_there_is_data(self):
        self.add("call.recorded", {"title": "ACME :: t :: SP"})
        self.add("hours.recorded", {"estimate_hours": 100, "actual_hours": 130})
        self.add("defect.found", {"phase": "uat"})
        self.add("ai.used", {"action": "draft_pack", "model": "sonnet", "input_tokens": 9000, "output_tokens": 2000})
        self.add("score.disputed", {"check": "C01", "reason": "the client renamed it on purpose"})
        self.add("score.dispute_resolved", {"check": "C01", "outcome": "overturned", "note": "agreed, the client renamed it"})
        p = self.page()
        self.assertIn("30.0%", p)
        self.assertIn("11,000", p)
        self.assertIn("draft_pack", p)
        self.assertIn("Disputed scores", p)
        self.assertIn("overturned by a", p)
        self.assertIn('<span class="pill na">-*</span>', p)      # the disputed (and overturned) cell is starred

    def test_the_mapping_section_refuses_to_conclude_from_too_little(self):
        self.add("call.recorded", {"title": "ACME :: t :: SP"})
        self.assertIn("Not enough finished flows", self.page())

    def test_the_sample_dashboard_shows_the_mapping_with_its_caveat(self):
        store = open_store(ROOT / "examples" / "events_sample.jsonl")
        p = render_dashboard(self.cfg, store, RULES, NOW, "SYNTHETIC DEMO DATA")
        self.assertIn("Rank correlation", p)
        self.assertIn("Correlation is not proof", p)
        self.assertEqual(len(re.findall(r"SP-REQ-0\d\d</td><td>build", p)), 12)

    def test_the_server_serves_it_behind_the_token(self):
        self.add("call.recorded", {"title": "x :: y :: SP"})
        ctx = Context(self.d / "projects", self.d / "data", RULES, api_token="t")
        self.assertEqual(handle(ctx, "GET", "/dashboard?project=SP", {})[0], 401)
        s, ctype, body = handle(ctx, "GET", "/dashboard?project=SP", {"Authorization": "Bearer t"})
        self.assertEqual((s, ctype), (200, "text/html; charset=utf-8"))
        self.assertIn(b"Delivery governance", body)


if __name__ == "__main__":
    unittest.main()
