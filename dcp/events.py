"""Append-only, hash-chained event log. Every governance number is computed from this.

One JSON object per line. Each event carries the hash of the previous one, so editing or
deleting history after the fact is detectable (`verify`). Adapters (Pulse, GitHub, Slack,
Granola) all write through `EventStore.append`, so scoring never depends on who is asking.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

try:
    import fcntl
except ImportError:  # non-POSIX: no cross-process locking
    fcntl = None

TRACKS = ("build", "support")

# event type -> data keys that must be present
EVENT_TYPES: Dict[str, List[str]] = {
    # build track: call to release
    "call.recorded": ["title"],
    "pack.drafted": ["version"],
    "pack.linted": ["version", "passed", "blocks", "warns"],
    "questions.sent": ["count"],
    "gate.opened": ["gate"],
    "gate.approved": ["gate", "approver", "approver_role"],
    "gate.rejected": ["gate", "reason"],
    "tickets.created": ["count"],
    "tickets.linted": ["passed", "blocks", "warns"],
    "drift.checked": ["passed", "blocks"],
    "build.started": ["ticket"],
    "assumptions.received": ["ticket"],
    "release.demoed": ["against_criteria"],
    # both tracks: change and review
    "tier.set": ["tier", "actor_role"],
    "pr.opened": ["pr", "author", "head_sha"],
    "pr.pushed": ["pr", "head_sha"],
    "pr.reviewed": ["pr", "reviewer", "state"],
    "parakh.verdict": ["pr", "head_sha", "gate"],
    "pr.merged": ["pr", "merger", "merger_role", "approvers"],
    # support track
    "ticket.created": ["ticket"],
    # exceptions
    "breakglass.used": ["reason"],
    "breakglass.reviewed": ["reviewer"],
    # orchestration
    "call.retitled": ["title"],
    "call.assessed": ["covered", "missing", "ready"],
    "call.override": ["reason"],
    "call.followup": ["title", "source_ref"],
    "pack.reopened": ["reason", "from_version"],
    "intake.received": ["source_ref", "text"],
    "tickets.drafted": ["count"],
    "pulse.mapped": ["local_id", "key"],
    "tech.sized": ["sizes"],
    "decision.closed": ["titles"],
    "ai.used": ["action", "model", "input_tokens", "output_tokens"],
    "score.disputed": ["check", "reason"],
    "score.dispute_resolved": ["check", "outcome", "note"],
    "branch.shared": ["repo", "branch"],
    "code.not_needed": ["reason"],
    "brief.built": ["path"],
    "poll.ran": ["stage"],
    "nudge.sent": ["check"],
    "escalated": ["reason"],
    # outcomes (feed the efficiency mapping)
    "ticket.reopened": ["ticket"],
    "change_request.raised": ["reason"],
    "defect.found": ["phase"],
    "assumption_gap.found": ["phase"],
    "hours.recorded": ["estimate_hours", "actual_hours"],
}


class EventError(ValueError):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Event:
    id: str
    ts: datetime
    type: str
    flow: str
    project: str
    track: str
    actor: str
    data: dict


def _digest(prev: str, body: dict) -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()


class EventStore:
    """Append-only, hash-chained log in a text file (one JSON object per line). The pilot backend.
    Subclasses change only where the lines live (`_locked`, `_lines`, `_write_line`); everything else, including the
    chain and validation, is shared, so every backend behaves identically."""

    def __init__(self, path):
        self.path = Path(path)

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "w") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _write_line(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _lines(self) -> List[str]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]

    def append(self, type: str, flow: str, project: str, actor: str,
               data: Optional[dict] = None, ts=None, track: str = "build") -> Event:
        data = dict(data or {})
        if type not in EVENT_TYPES:
            raise EventError(f"unknown event type '{type}'")
        missing = [k for k in EVENT_TYPES[type] if k not in data]
        if missing:
            raise EventError(f"'{type}' needs data keys: {', '.join(missing)}")
        if track not in TRACKS:
            raise EventError(f"track must be one of {TRACKS}")
        if not flow or not project or not actor:
            raise EventError("flow, project and actor are required")

        when = parse_ts(ts) if ts else now_utc()
        with self._locked():
            lines = self._lines()
            prev = json.loads(lines[-1])["hash"] if lines else ""
            body = {
                "id": uuid.uuid4().hex[:12],
                "ts": fmt_ts(when),
                "type": type,
                "flow": flow,
                "project": project,
                "track": track,
                "actor": actor,
                "data": data,
                "prev": prev,
            }
            body["hash"] = _digest(prev, {k: v for k, v in body.items() if k != "hash"})
            self._write_line(json.dumps(body, ensure_ascii=False))
        return self._to_event(body)

    @staticmethod
    def _to_event(raw: dict) -> Event:
        return Event(raw["id"], parse_ts(raw["ts"]), raw["type"], raw["flow"], raw["project"],
                     raw["track"], raw["actor"], raw["data"])

    def events(self, flow: Optional[str] = None, project: Optional[str] = None,
               type: Optional[str] = None) -> List[Event]:
        out = []
        for ln in self._lines():
            raw = json.loads(ln)
            if flow and raw["flow"] != flow:
                continue
            if project and raw["project"] != project:
                continue
            if type and raw["type"] != type:
                continue
            out.append(self._to_event(raw))
        out.sort(key=lambda e: e.ts)
        return out

    def flows(self, project: Optional[str] = None) -> List[str]:
        """Real flows only. Names starting with '_' (e.g. '_poll') are system bookkeeping."""
        seen: Dict[str, None] = {}
        for e in self.events(project=project):
            if not e.flow.startswith("_"):
                seen.setdefault(e.flow, None)
        return list(seen)

    def head(self):
        """(number of events, hash of the last one). Publish this somewhere other people can see (a daily Slack post, a Git
        commit): then even a consistent rewrite of the whole chain is detectable, because the published hash will not match."""
        lines = self._lines()
        return len(lines), (json.loads(lines[-1])["hash"] if lines else "")

    def check_anchor(self, count: int, expected: str) -> Optional[str]:
        """None if the event at position `count` still has the hash that was published, else why not."""
        lines = self._lines()
        if count < 1 or count > len(lines):
            return f"the log has {len(lines)} events but an anchor was published at event {count}: events were removed"
        got = json.loads(lines[count - 1])["hash"]
        return None if got == expected else (f"event {count} no longer has the published hash: the history before it was rewritten")

    def verify(self) -> List[str]:
        """Returns a list of problems; empty means the chain is intact."""
        problems = []
        prev = ""
        for i, ln in enumerate(self._lines(), start=1):
            try:
                raw = json.loads(ln)
            except json.JSONDecodeError:
                problems.append(f"line {i}: not valid JSON")
                break
            if raw.get("prev") != prev:
                problems.append(f"line {i}: chain broken (an earlier event was changed or removed)")
            expect = _digest(raw.get("prev", ""), {k: v for k, v in raw.items() if k != "hash"})
            if raw.get("hash") != expect:
                problems.append(f"line {i}: event {raw.get('id')} was altered")
            prev = raw.get("hash", "")
        return problems


class SqliteEventStore(EventStore):
    """The same log in SQLite: safe for several writers, easy to back up, queryable. One row per event holding the
    exact JSON line, so a log can be moved between backends without changing a single hash."""

    def __init__(self, path):
        super().__init__(path)
        self._tx: Optional[sqlite3.Connection] = None

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, line TEXT NOT NULL)")
        return conn

    @contextlib.contextmanager
    def _locked(self):
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        self._tx = conn
        try:
            yield
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            self._tx = None
            conn.close()

    def _write_line(self, line: str) -> None:
        self._tx.execute("INSERT INTO events (line) VALUES (?)", (line,))

    def _lines(self) -> List[str]:
        if self._tx is not None:
            rows = self._tx.execute("SELECT line FROM events ORDER BY seq").fetchall()
        else:
            if not self.path.exists():
                return []
            conn = self._connect()
            try:
                rows = conn.execute("SELECT line FROM events ORDER BY seq").fetchall()
            finally:
                conn.close()
        return [r[0] for r in rows if r[0].strip()]


def open_store(path) -> EventStore:
    """Chooses the backend from the file name: .db / .sqlite is SQLite, anything else the text file."""
    return SqliteEventStore(path) if str(path).lower().endswith((".db", ".sqlite")) else EventStore(path)


def copy_store(src: EventStore, dst: EventStore) -> int:
    """Copies every line verbatim, so the hash chain survives the move. The destination must be empty."""
    if dst._lines():
        raise EventError("the destination log is not empty")
    n = 0
    with dst._locked():
        for ln in src._lines():
            dst._write_line(ln)
            n += 1
    return n
