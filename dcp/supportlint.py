"""Support (L2 / incident) tickets: same idea as the build track, lighter. A request that arrives in an
existing Slack channel becomes a Pulse ticket in a fixed structure, drafted by Claude from the thread
and checked here before anything is written."""
from __future__ import annotations

import re
from typing import Dict, List

from .findings import BLOCK, WARN, Collector, LintResult
from .flowstate import suggest_tier

FIELDS = [
    ("what_happened", "What happened"),
    ("impact", "Impact"),
    ("environment", "Environment"),
    ("evidence", "Evidence"),
    ("reported_by", "Reported by"),
    ("slack_thread", "Slack thread"),
    ("tier", "Tier (suggested)"),
    ("next_step", "Next step"),
]
_NOT_STATED = re.compile(r"^\W*(not stated|unknown|tbd|n/?a|\?)\W*$", re.I)
_CRIT = ("down", "outage", "cannot checkout", "can't checkout", "payment fail", "breach", "security", "data loss", "all orders")
_HIGH = ("not working", "failed", "failing", "error", "blocked", "urgent", "cannot", "can't", "wrong amount", "not received")
_BUGLIKE = ("error", "not working", "failed", "failing", "wrong", "bug", "broken", "not received", "not showing", "issue")


def suggest_priority(text: str) -> str:
    t = text.lower()
    if any(w in t for w in _CRIT):
        return "critical"
    if any(w in t for w in _HIGH):
        return "high"
    return "medium"


def suggest_type(text: str) -> str:
    return "bug" if any(w in text.lower() for w in _BUGLIKE) else "task"


def lint_support_fields(fields: Dict[str, str], rules: dict) -> LintResult:
    c = Collector("support ticket", rules["lint"].get("severity_overrides"))
    for key, label in FIELDS:
        v = (fields.get(key) or "").strip()
        if not v:
            sev = WARN if key in ("evidence", "next_step") else BLOCK
            c.add("SU001", sev, f"Missing '{label}'.")
        elif _NOT_STATED.match(v):
            c.add("SU002", WARN, f"'{label}' is '{v}'. Ask the reporter in the thread before the ticket goes to a developer.")
    if fields.get("slack_thread") and not re.match(r"^https?://", fields["slack_thread"].strip()):
        c.add("SU003", WARN, "Slack thread should be a link so the source is one click away.")
    if (fields.get("tier") or "").strip().upper() not in ("T1", "T2", "T3"):
        c.add("SU004", BLOCK, "Tier must be T1, T2 or T3 (suggested; the tech lead confirms).")
    return c.result


def build_support_ticket(fields: Dict[str, str], title: str, prefix: str, rules: dict, flow: str) -> dict:
    res = lint_support_fields(fields, rules)
    text = f"{title} {fields.get('what_happened', '')} {fields.get('impact', '')}"
    lines: List[str] = []
    for key, label in FIELDS:
        v = (fields.get(key) or "").strip()
        if v:
            lines.append(f"{label}: {v}" if "\n" not in v else f"{label}:\n{v}")
    return {
        "lint": res.to_dict(),
        "create": {
            "type": suggest_type(text),
            "title": f"{prefix} {title}".strip()[:500],
            "priority": suggest_priority(text),
            "description": "\n".join(lines) + f"\n\n---\ndcp-id: {flow}",
        },
        "suggested_tier": suggest_tier(text),
    }
