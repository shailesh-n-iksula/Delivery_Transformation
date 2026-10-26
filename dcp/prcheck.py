"""The pull request check, to run in CI as a required status check. It reads the GitHub event, so it needs no tokens.

It enforces the same rules the delivery system scores, at the moment they can still be fixed: before merge.
It cannot know the tier a lead set (that lives in the event log), so it is deliberately strict when the PR is silent.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from .webhooks import parse_pr_body

_REF = re.compile(r"^(feature|bugfix|hotfix)/([A-Z][A-Z0-9]{1,9}-\d+)-[a-z0-9][a-z0-9-]*$")


def check_pr(event: dict, key: str, base: str) -> List[dict]:
    pr = event.get("pull_request") or {}
    body, ref, sha = pr.get("body") or "", (pr.get("head") or {}).get("ref", ""), (pr.get("head") or {}).get("sha", "")
    info = parse_pr_body(body)
    out: List[dict] = []

    def add(ok, label, why, fix=""):
        out.append({"ok": ok, "label": label, "why": why, "fix": "" if ok else fix})

    m = _REF.match(ref)
    add(bool(m) and m.group(2).startswith(key + "-"), "Branch follows GitHub flow",
        ref or "(no branch)", f"Name it feature/, bugfix/ or hotfix/ + a {key} ticket key + a short description, e.g. feature/{key}-123-returns-export.")
    add((pr.get("base") or {}).get("ref") == base, f"Targets '{base}'", (pr.get("base") or {}).get("ref", "?"), f"Change the base branch to {base}.")
    ticket_ok = bool(info["ticket"]) and (not m or info["ticket"] == m.group(2))
    add(ticket_ok, "Names its ticket, and it matches the branch", info["ticket"] or "(none)",
        "Fill in the 'Ticket:' line with the Pulse key from the branch name.")
    add(info["ai_declared"], "Declares AI use (yes or no)", "present" if info["ai_declared"] else "missing", "Answer the 'AI-assisted:' line with yes or no.")
    add(info["explains_every_line"], "Maker confirms they can explain every line", "ticked" if info["explains_every_line"] else "not ticked",
        "Tick 'I can explain every line' only if it is true.")
    tier = info["tier"]
    add(bool(tier), "Declares a tier", tier or "(none)", "Fill in the 'Tier:' line (T1, T2 or T3). The tech lead sets the real tier on the ticket.")
    if tier == "T1" or not tier:
        add(bool(info["failure_path_test"]), "T1: failure-path test described", "present" if info["failure_path_test"] else "missing",
            "Describe the failure-path test (what happens when the gateway, ERP or network fails).")
        add(bool(info["rollback_note"]), "T1: rollback note", "present" if info["rollback_note"] else "missing", "Write exactly how to undo this in production.")
    if tier != "T3":
        pk = info["parakh"]
        add(bool(pk), "Parakh verdict pasted", "present" if pk else "missing", "Run /parakh and paste its footer line into the description.")
        if pk:
            add(pk["head_sha"] == sha, "Parakh verdict is for the latest commit", f"verdict {pk['head_sha'][:7]}, head {sha[:7]}",
                "Run /parakh again on the latest commit and update the footer.")
            add(str(pk["gate"]).lower() == "pass", "Parakh gate is a pass", str(pk["gate"]), "Fix the blockers Parakh found, then run it again.")
    return out


def render(results: List[dict]) -> str:
    bad = [r for r in results if not r["ok"]]
    lines = [f"{'PASS' if not bad else 'FAIL'}: {len(results) - len(bad)} of {len(results)} checks"]
    for r in results:
        lines.append(f"  {'ok  ' if r['ok'] else 'FAIL'} {r['label']} ({r['why']})" + (f"\n       fix: {r['fix']}" if r["fix"] else ""))
    return "\n".join(lines)
