import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dcp.cli import main
from dcp.config import load_rules
from dcp.events import EventError, EventStore, SqliteEventStore, copy_store, open_store
from dcp.scoring import evaluate_store

import contextlib
import io

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules()
T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


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


class BackendContract:
    """Every backend must pass these. A new backend (Postgres, ...) subclasses this and sets `suffix`."""
    suffix = ""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / ("events" + self.suffix)
        self.store = open_store(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def add(self, type_, data, minutes=0, flow="F1"):
        return self.store.append(type_, flow, "SP", "a", data, ts=T0 + timedelta(minutes=minutes))

    def test_factory_picks_the_right_backend(self):
        self.assertIsInstance(self.store, self.expected_class)

    def test_empty_log(self):
        self.assertEqual((self.store.events(), self.store.flows(), self.store.verify()), ([], [], []))

    def test_append_validates_like_every_other_backend(self):
        for bad in (lambda: self.add("nonsense", {}), lambda: self.add("call.recorded", {}),
                    lambda: self.store.append("call.recorded", "", "SP", "a", {"title": "x"})):
            with self.assertRaises(EventError):
                bad()
        self.assertEqual(self.store.events(), [])

    def test_order_filters_and_system_flows(self):
        self.add("pack.drafted", {"version": 1}, 5)
        self.add("call.recorded", {"title": "x"}, 0)
        self.add("call.recorded", {"title": "y"}, 1, flow="F2")
        self.add("poll.ran", {"stage": "slack"}, 2, flow="_poll")
        self.assertEqual([e.type for e in self.store.events(flow="F1")], ["call.recorded", "pack.drafted"])
        self.assertEqual(self.store.flows(), ["F1", "F2"])          # '_poll' is bookkeeping, not a flow
        self.assertEqual(len(self.store.events(type="call.recorded")), 2)
        self.assertEqual(self.store.events(flow="F1")[0].data, {"title": "x"})

    def test_chain_is_intact_and_each_event_links_to_the_last(self):
        for i in range(5):
            self.add("pack.drafted", {"version": i}, i)
        self.assertEqual(self.store.verify(), [])
        lines = self.store._lines()
        self.assertEqual(json.loads(lines[3])["prev"], json.loads(lines[2])["hash"])

    def test_concurrent_writers_do_not_break_the_chain(self):
        def writer(n):
            s = open_store(self.path)
            for i in range(10):
                s.append("pack.drafted", f"F{n}", "SP", "a", {"version": i})
        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(self.store.events()), 40)
        self.assertEqual(self.store.verify(), [])

    def test_scoring_runs_on_this_backend(self):
        self.add("call.recorded", {"title": "ACME :: t :: SP"})
        r = evaluate_store(self.store, RULES, flow="F1")[0]
        self.assertEqual(next(c.status for c in r.checks if c.id == "C01"), "pass")


class JsonlBackendTests(BackendContract, unittest.TestCase):
    suffix = ".jsonl"
    expected_class = EventStore

    def test_editing_a_line_is_detected(self):
        self.add("call.recorded", {"title": "x"})
        self.add("pack.drafted", {"version": 1}, 1)
        self.path.write_text(self.path.read_text().replace('"version": 1', '"version": 9'))
        self.assertTrue(self.store.verify())

    def test_deleting_a_line_is_detected(self):
        for i in range(3):
            self.add("pack.drafted", {"version": i}, i)
        lines = self.path.read_text().splitlines()
        self.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
        self.assertTrue(self.store.verify())


class SqliteBackendTests(BackendContract, unittest.TestCase):
    suffix = ".db"
    expected_class = SqliteEventStore

    def test_editing_a_row_is_detected(self):
        self.add("call.recorded", {"title": "x"})
        self.add("pack.drafted", {"version": 1}, 1)
        con = sqlite3.connect(self.path)
        con.execute("UPDATE events SET line = REPLACE(line, '\"version\": 1', '\"version\": 9') WHERE seq = 2")
        con.commit()
        con.close()
        self.assertTrue(self.store.verify())

    def test_deleting_a_row_is_detected(self):
        for i in range(3):
            self.add("pack.drafted", {"version": i}, i)
        con = sqlite3.connect(self.path)
        con.execute("DELETE FROM events WHERE seq = 2")
        con.commit()
        con.close()
        self.assertTrue(self.store.verify())

    def test_a_failed_write_rolls_back_and_leaves_the_log_usable(self):
        self.add("call.recorded", {"title": "x"})
        with self.assertRaises(EventError):
            self.store.append("call.recorded", "F1", "SP", "a", {})
        self.add("pack.drafted", {"version": 1}, 1)
        self.assertEqual((len(self.store.events()), self.store.verify()), (2, []))


class AnchorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "e.jsonl"
        self.store = open_store(self.path)
        for i in range(5):
            self.store.append("pack.drafted", "F", "SP", "a", {"version": i}, ts=T0 + timedelta(minutes=i))

    def tearDown(self):
        self.dir.cleanup()

    def rewrite_consistently(self):
        """What a determined insider with file access could do: change an early event and recompute every later hash."""
        from dcp.events import _digest
        lines = [json.loads(l) for l in self.path.read_text().splitlines()]
        lines[1]["data"]["version"] = 99
        prev = lines[0]["hash"]
        for rec in lines[1:]:
            rec["prev"] = prev
            rec["hash"] = _digest(prev, {k: v for k, v in rec.items() if k != "hash"})
            prev = rec["hash"]
        self.path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")

    def test_head_gives_the_anchor_to_publish(self):
        n, h = self.store.head()
        self.assertEqual((n, len(h)), (5, 64))
        code, out = run("log-head", str(self.path), "--short")
        self.assertEqual(out.strip(), f"{n}:{h}")
        self.assertEqual(open_store(Path(self.dir.name) / "empty.jsonl").head(), (0, ""))

    def test_a_consistent_rewrite_passes_the_chain_check_but_not_the_published_anchor(self):
        n, h = self.store.head()
        anchor = f"{n}:{h}"
        self.rewrite_consistently()
        self.assertEqual(self.store.verify(), [])                       # the chain alone cannot tell
        code, out = run("verify-log", str(self.path), "--anchor", anchor)
        self.assertEqual(code, 1)
        self.assertIn("rewritten", out)

    def test_an_untouched_log_matches_its_anchor_and_new_events_do_not_break_it(self):
        n, h = self.store.head()
        self.store.append("pack.drafted", "F", "SP", "a", {"version": 9}, ts=T0 + timedelta(hours=1))
        code, out = run("verify-log", str(self.path), "--anchor", f"{n}:{h}")
        self.assertEqual((code, out.strip()), (0, "log intact"))

    def test_truncation_and_malformed_anchors_are_caught(self):
        n, h = self.store.head()
        lines = self.path.read_text().splitlines()
        self.path.write_text("\n".join(lines[:3]) + "\n")
        self.assertIn("removed", self.store.check_anchor(n, h))
        self.assertEqual(run("verify-log", str(self.path), "--anchor", "nonsense")[0], 1)

    def test_it_works_on_sqlite_too(self):
        s = open_store(Path(self.dir.name) / "e.db")
        copy_store(self.store, s)
        n, h = s.head()
        self.assertIsNone(s.check_anchor(n, h))
        self.assertIsNotNone(s.check_anchor(n, "0" * 64))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        shutil.copy(ROOT / "projects" / "SP.json", d / "projects" / "SP.json")
        self.d = d
        self.base = ["--projects-dir", str(d / "projects"), "--data-dir", str(d / "data")]
        s = open_store(d / "data" / "SP" / "events.jsonl")
        for i in range(6):
            s.append("pack.drafted", "SP-REQ-001", "SP", "a", {"version": i}, ts=T0 + timedelta(minutes=i))

    def tearDown(self):
        self.dir.cleanup()

    def test_the_log_moves_with_every_hash_unchanged_and_the_project_follows(self):
        before = open_store(self.d / "data" / "SP" / "events.jsonl")._lines()
        code, out = run("migrate-store", "SP", "--to", "sqlite", "--projects-dir", str(self.d / "projects"),
                        "--data-dir", str(self.d / "data"))
        self.assertEqual(code, 0, out)
        after = open_store(self.d / "data" / "SP" / "events.db")._lines()
        self.assertEqual(before, after)
        self.assertEqual(json.loads((self.d / "projects" / "SP.json").read_text())["storage"]["backend"], "sqlite")
        self.assertTrue((self.d / "data" / "SP" / "events.jsonl").exists())        # old log kept as a backup
        self.assertEqual(len(json.loads(run("next", "--project", "SP", *self.base, "--json")[1])["orders"]) >= 0, True)

    def test_commands_follow_the_configured_backend(self):
        run("migrate-store", "SP", "--to", "sqlite", "--projects-dir", str(self.d / "projects"), "--data-dir", str(self.d / "data"))
        run("flow-new", "--project", "SP", *self.base, "--source-ref", "g", "--title", "S :: t :: SP")
        flows = open_store(self.d / "data" / "SP" / "events.db").flows()
        self.assertIn("SP-REQ-002", flows)       # SP-REQ-001 was already used by the test fixture? no: new flow id is unique
        self.assertFalse(any(e.type == "call.recorded" for e in open_store(self.d / "data" / "SP" / "events.jsonl").events()))

    def test_it_can_move_back_and_refuses_a_pointless_or_tampered_move(self):
        a = ["--projects-dir", str(self.d / "projects"), "--data-dir", str(self.d / "data")]
        self.assertEqual(run("migrate-store", "SP", "--to", "jsonl", *a)[0], 1)         # already jsonl
        p = self.d / "data" / "SP" / "events.jsonl"
        p.write_text(p.read_text().replace('"version": 2', '"version": 7'))
        code, out = run("migrate-store", "SP", "--to", "sqlite", *a)
        self.assertEqual(code, 1)
        self.assertIn("integrity", out)
        self.assertFalse((self.d / "data" / "SP" / "events.db").exists() and open_store(self.d / "data" / "SP" / "events.db").events())

    def test_copy_refuses_a_non_empty_destination(self):
        src = open_store(self.d / "data" / "SP" / "events.jsonl")
        dst = open_store(self.d / "x.db")
        copy_store(src, dst)
        with self.assertRaises(EventError):
            copy_store(src, dst)


if __name__ == "__main__":
    unittest.main()
