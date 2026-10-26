"""Turns a lint-clean ticket set into ordered Pulse operations, in the agreed structure.

The Pulse connector can create only story / task / bug issues, with a plain-text description.
No epic type, parent link, label, sprint or assignee. So the structure is carried by convention:

  * title prefix:  EPIC: / DEP: / RISK: / DECISION:   (stories and bugs keep their own titles)
  * description:   the same 'Label: value' fields the linters read, so a ticket read back from
                   Pulse can be linted again (Definition-of-Ready sweep, drift detection)
  * links:         a second pass of comments ('Parent epic: ML-12', 'Blocked by: ML-14') once keys exist
  * marker:        'dcp-id: <flow>/<local id>' footer, so a re-run never creates a duplicate

The plan is resumable: it is recomputed from the event log, and only operations not yet done are
returned. Replace the epic/parent conventions with real fields once the Pulse API exposes them.
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional

from .mdparse import numbered_items, similarity
from .ticketlint import Ticket, parse_tickets, ref_key

_KEY_REF = re.compile(r"\{\{key:([a-z0-9]+)\}\}")
_SIZE = re.compile(r"\b(s\d+)\s*=\s*(\d+(?:\.\d+)?)\s*(d|days?|h|hrs?|hours?)\b", re.I)

FIELD_ORDER = {
    "EPIC": ["Why", "Links"],
    "STORY": ["Why", "Scope in", "Scope out", "Acceptance criteria", "Client's words", "Technical context",
              "Links", "Answers from the client", "Assumptions", "Blocked by"],
    "DEP": ["Owner", "Needed by", "What stops if late", "Blocks"],
    "RISK": ["Impact", "Mitigation", "Owner", "Review date"],
    "DECISION": ["Who decides", "Options", "Cost of deciding late", "Status"],
}
PREFIX = {"EPIC": "EPIC: ", "DEP": "DEP: ", "RISK": "RISK: ", "DECISION": "DECISION: ", "STORY": ""}
# Titles keep the prefix permanently. The label is ADDED once the Pulse API can set labels; until then it is
# also written as a 'Label:' line at the top of the description so it is visible and searchable.
LABEL_FOR = {"DEP": "dependency", "RISK": "risk", "DECISION": "decision"}


class PlanError(ValueError):
    pass


def assign_local_ids(tickets: List[Ticket]) -> Dict[int, str]:
    """Stable ids by kind and order: epic, s1.., d1.., r1.., x1.. (x = decision)."""
    counters = {"STORY": 0, "DEP": 0, "RISK": 0, "DECISION": 0, "REMOVED": 0}
    letter = {"STORY": "s", "DEP": "d", "RISK": "r", "DECISION": "x", "REMOVED": "rmv"}
    ids: Dict[int, str] = {}
    for i, t in enumerate(tickets):
        if t.kind == "EPIC":
            ids[i] = "epic"
        else:
            counters[t.kind] += 1
            ids[i] = f"{letter[t.kind]}{counters[t.kind]}"
    return ids


def render_description(t: Ticket, flow: str, local_id: str, pack_ref: str) -> str:
    lines: List[str] = []
    for label in FIELD_ORDER[t.kind]:
        if label == "Blocked by":
            continue  # keys are not known yet; the link pass writes this as a comment
        val = t.get(label)
        if not val:
            continue
        if "\n" in val.strip():
            lines.append(f"{label}:")
            lines.extend(l.rstrip() for l in val.strip("\n").splitlines())
        else:
            lines.append(f"{label}: {val.strip()}")
    if t.kind in LABEL_FOR:
        lines.insert(0, f"Label: {LABEL_FOR[t.kind]}")
    body = "\n".join(lines)
    # The pack version in the Links line changes on every revision. It is not a change to the ticket, so it is left out of the
    # fingerprint; otherwise one change request would mark every ticket as changed.
    stable = "\n".join(re.sub(r"\bv\d+\b", "v#", l) if l.startswith("Links:") else l for l in lines)
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:8]
    return f"{body}\n\n---\ndcp-id: {flow}/{local_id} | {pack_ref} | hash:{digest}"


def _priority(t: Ticket, tier: Optional[str]) -> str:
    if t.kind in ("DEP", "RISK", "DECISION"):
        return "high"
    if t.kind == "STORY" and tier == "T1":
        return "high"
    return "medium"


def parse_sizes(text: str) -> Dict[str, str]:
    """'S1=5d s2=3 days S3=4h' -> {'s1': '5 days', 's2': '3 days', 's3': '4 hours'}"""
    out: Dict[str, str] = {}
    for sid, num, unit in _SIZE.findall(text):
        out[sid.lower()] = f"{num} {'days' if unit.lower().startswith('d') else 'hours'}"
    return out


def apply_updates(tickets_md: str, sizes: Dict[str, str], closed_decisions: List[str]) -> str:
    """Merge the tech lead's sizes and closed decisions into the local ticket-set markdown, so the
    ready-stage lint (which requires Size and closed decisions) can run."""
    tickets = parse_tickets(tickets_md)
    ids = assign_local_ids(tickets)
    by_line = {t.line: (i, t) for i, t in enumerate(tickets)}
    out: List[str] = []
    pending_size: Optional[str] = None
    lines = tickets_md.splitlines()
    for n, line in enumerate(lines, 1):
        if n in by_line:
            i, t = by_line[n]
            pending_size = sizes.get(ids[i]) if t.kind == "STORY" else None
        if re.match(r"^\s*Size\s*:", line, re.I) and pending_size:
            line = f"Size: {pending_size} (tech lead)"
            pending_size = None
        out.append(line)
    text = "\n".join(out) + "\n"
    # add missing Size lines after the last field of the story, and close decisions
    tickets = parse_tickets(text)
    ids = assign_local_ids(tickets)
    result: List[str] = []
    blocks = [t.line for t in tickets] + [len(text.splitlines()) + 1]
    all_lines = text.splitlines()
    for idx, t in enumerate(tickets):
        seg = all_lines[t.line - 1: blocks[idx + 1] - 1]
        body = "\n".join(seg)
        if t.kind == "STORY" and ids[idx] in sizes and not re.search(r"^\s*Size\s*:", body, re.M | re.I):
            while seg and not seg[-1].strip():
                seg.pop()
            seg.append(f"Size: {sizes[ids[idx]]} (tech lead)")
            seg.append("")
        if t.kind == "DECISION" and any(ref_key(c) in ref_key(t.title) or ref_key(t.title) in ref_key(c)
                                        for c in closed_decisions if c.strip()):
            if re.search(r"^\s*Status\s*:", body, re.M | re.I):
                seg = [re.sub(r"^(\s*Status\s*:).*$", r"\1 Closed", l, flags=re.I) for l in seg]
            else:
                while seg and not seg[-1].strip():
                    seg.pop()
                seg.extend(["Status: Closed", ""])
        result.extend(seg)
    head = all_lines[: tickets[0].line - 1] if tickets else all_lines
    return "\n".join(head + result) + "\n"


_HASH = re.compile(r"hash:([0-9a-f]{8})")


def build_plan(tickets_md: str, flow: str, pack_ref: str, project_key: str,
               mapped: Dict[str, str], tier: Optional[str] = None,
               sizes: Optional[Dict[str, str]] = None, hashes: Optional[Dict[str, str]] = None,
               titles: Optional[Dict[str, str]] = None, version: Optional[int] = None) -> dict:
    """mapped: local_id -> Pulse key for everything already created or commented.
    Returns the operations still to do whose prerequisites exist, plus counts."""
    all_tickets = parse_tickets(tickets_md)
    removed = [t for t in all_tickets if t.kind == "REMOVED"]
    tickets = [t for t in all_tickets if t.kind != "REMOVED"]
    if not tickets:
        raise PlanError("no tickets found in the ticket set")
    ids = assign_local_ids(tickets)
    local_of = {ids[i]: t for i, t in enumerate(tickets)}
    story_ids = [ids[i] for i, t in enumerate(tickets) if t.kind == "STORY"]
    link_ids = {ref_key(t.title): ids[i] for i, t in enumerate(tickets) if t.kind in ("DEP", "RISK", "DECISION")}

    creates, comments = [], []
    warnings: List[str] = []
    hashes, titles = hashes or {}, titles or {}
    for i, t in enumerate(tickets):
        lid = ids[i]
        creates.append({
            "op": "create", "local_id": lid, "needs": [],
            "projectKey": project_key,
            "type": "task" if t.kind in ("DEP", "RISK", "DECISION") else "story",
            "title": (PREFIX[t.kind] + t.title)[:500],
            "priority": _priority(t, tier),
            "labels": [LABEL_FOR[t.kind]] if t.kind in LABEL_FOR else [],
            "desired_type": "epic" if t.kind == "EPIC" else None,
            "description": render_description(t, flow, lid, pack_ref),
        })

    epic = "epic" if "epic" in local_of else None
    blocked_by: Dict[str, List[str]] = {}
    for sid in story_ids:
        refs = [r.strip(" -*\t") for r in re.split(r"[\n;]", local_of[sid].get("blocked by")) if r.strip(" -*\t")]
        blocked_by[sid] = [link_ids[ref_key(r)] for r in refs if ref_key(r) in link_ids]

    if epic:
        kids = story_ids + sorted(link_ids.values())
        comments.append({"op": "comment", "local_id": "c:epic", "on": "epic", "needs": ["epic"] + kids,
                         "body": "Children: " + ", ".join("{{key:%s}}" % k for k in kids)})
    for sid in story_ids:
        lines = []
        needs = [sid]
        if epic:
            lines.append("Parent epic: {{key:epic}}")
            needs.append("epic")
        bl = blocked_by.get(sid, [])
        lines.append("Blocked by: " + (", ".join("{{key:%s}}" % b for b in bl) if bl else "none"))
        needs += bl
        comments.append({"op": "comment", "local_id": f"c:{sid}", "on": sid, "needs": needs, "body": "\n".join(lines)})
    for lid in sorted(link_ids.values()):
        blocks = [sid for sid, bl in blocked_by.items() if lid in bl]
        lines, needs = [], [lid]
        if epic:
            lines.append("Parent epic: {{key:epic}}")
            needs.append("epic")
        lines.append("Blocks: " + (", ".join("{{key:%s}}" % b for b in blocks) if blocks else "none"))
        needs += blocks
        comments.append({"op": "comment", "local_id": f"c:{lid}", "on": lid, "needs": needs, "body": "\n".join(lines)})
    for sid, size in sorted((sizes or {}).items()):
        if sid in local_of:
            comments.append({"op": "comment", "local_id": f"size:{sid}", "on": sid, "needs": [sid],
                             "body": f"Size (tech lead): {size}"})

    # A requirement change: tickets already in Pulse whose content changed get a change note (and the new text ready
    # for an update call once the API allows one); tickets no longer in the set get a removal note.
    for op in creates:
        lid = op["local_id"]
        new_hash = (_HASH.search(op["description"]) or [None, ""])[1]
        op["hash"] = new_hash
        if lid in mapped and hashes.get(lid) and hashes[lid] != new_hash:
            comments.append({"op": "comment", "local_id": f"chg:{lid}:{new_hash}", "on": lid, "needs": [lid],
                             "body": f"Requirement changed" + (f" (pack v{version})" if version else "")
                                     + f". New description:\n\n{op['description']}",
                             "needs_api_update": True, "update_to": {"title": op["title"], "description": op["description"]}})
        old_title = titles.get(lid)
        if lid in mapped and old_title and similarity(old_title, op["title"]) < 0.5:
            warnings.append(f"{lid} used to be '{old_title[:60]}' and is now '{op['title'][:60]}'. Do not reorder or "
                            "replace tickets when revising: keep their order and add new ones at the end.")
    gone = {lid: "" for lid in mapped if re.match(r"^(epic|[sdrx]\d+)$", lid) and lid not in local_of}
    for t in removed:
        best = max(((similarity(old, t.title), lid) for lid, old in titles.items() if lid in mapped), default=(0.0, None))
        if best[1] and best[0] >= 0.8:
            gone[best[1]] = t.get("why")
        else:
            warnings.append(f"REMOVED '{t.title[:60]}' does not match any ticket created in Pulse, so nothing was marked.")
    for lid, why in sorted(gone.items()):
        comments.append({"op": "comment", "local_id": f"rm:{lid}", "on": lid, "needs": [lid],
                         "body": "No longer in scope" + (f" (pack v{version})" if version else "")
                                 + (f": {why}" if why else ".") + " Close this ticket or confirm it is still wanted."})

    def resolve(body: str) -> str:
        return _KEY_REF.sub(lambda m: mapped[m.group(1)], body)

    ready: List[dict] = []
    for op in creates + comments:
        if op["local_id"] in mapped:
            continue
        if all(n in mapped for n in op["needs"]):
            op = dict(op)
            if op["op"] == "comment":
                op["on_key"] = mapped[op["on"]]
                op["body"] = resolve(op["body"])
            ready.append(op)
    creates_done = sum(1 for op in creates if op["local_id"] in mapped)
    total = len(creates) + len(comments)
    done = sum(1 for op in creates + comments if op["local_id"] in mapped)
    return {"flow": flow, "project": project_key, "warnings": warnings, "total_ops": total, "done_ops": done,
            "complete": done == total, "ops": ready,
            "creates_total": len(creates), "creates_done": creates_done,
            "note": "Execute each op, then record it: dcp tickets-record --local-id <id> --key <Pulse key>. "
                    "Re-run tickets-plan until complete."}


def mapped_from_events(events) -> Dict[str, str]:
    return {e.data["local_id"]: e.data["key"] for e in events if e.type == "pulse.mapped"}


def hashes_from_events(events) -> Dict[str, str]:
    """The content hash each ticket had when it was last created or changed in Pulse."""
    out: Dict[str, str] = {}
    for e in events:
        if e.type == "pulse.mapped" and e.data.get("hash"):
            out[e.data["local_id"]] = e.data["hash"]
        elif e.type == "pulse.mapped" and e.data["local_id"].startswith("chg:"):
            lid, h = e.data["local_id"].split(":")[1:3]
            out[lid] = h
    return out


def titles_from_events(events) -> Dict[str, str]:
    return {e.data["local_id"]: e.data["title"] for e in events if e.type == "pulse.mapped" and e.data.get("title")}


def pulse_items_to_markdown(items: List[dict]) -> str:
    """Pulse export ([{key,title,type,description}]) back into the ticket-set markdown so the same
    linters can run on live tickets. Titles carry their kind prefix; the key is kept in the header."""
    out: List[str] = []
    for it in items:
        title = it.get("title", "")
        m = re.match(r"^(EPIC|DEP|RISK|DECISION)\s*:\s*(.*)$", title, re.I)
        kind, bare = (m.group(1).upper(), m.group(2)) if m else ("STORY", title)
        desc = (it.get("description") or "").split("\n---\ndcp-id:")[0]
        out.append(f"### {kind}: {bare}\n{desc}\n")
    return "\n".join(out)
