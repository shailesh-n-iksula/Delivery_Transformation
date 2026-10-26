"""Pulse ticket-set linter: the playbook's story template and Definition of Ready, as code."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from .findings import BLOCK, INFO, WARN, Collector, LintResult
from .mdparse import normalize, numbered_items

_HEADER = re.compile(r"^#{2,4}\s*(EPIC|STORY|DEP(?:ENDENCY)?|RISK|DECISION|REMOVED)\s*:\s*(.+?)\s*$", re.I)
LABELS = [
    "Why", "Scope in", "Scope out", "Acceptance criteria", "Technical context", "Links",
    "Assumptions", "Blocked by", "Blocks", "Owner", "Needed by", "What stops if late",
    "Impact", "Mitigation", "Review date", "Who decides", "Options",
    "Cost of deciding late", "Size", "Status", "Epic", "Client's words", "Answers from the client",
]
_LABEL_LINE = re.compile(
    r"^\s*(?:[-*]\s+)?\**\s*(" + "|".join(re.escape(l) for l in sorted(LABELS, key=len, reverse=True))
    + r")\s*\**\s*:\s*\**\s*(.*)$", re.I)
_GWT = re.compile(r"\bgiven\b.+?\bwhen\b.+?\bthen\b", re.I | re.S)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_VOLUME = re.compile(
    r"\d[\d,.]*\s*[kKmM]?\s*(?:rows|orders|users|skus?|products?|requests?|records?|files?|items?|stores?|mb|gb|tps|rps|tickets?|events?|calls?|"
    r"pull requests?|messages?|flows?|projects?|emails?|transactions?|sessions?|transcripts?|drafts?|tokens?|customers?|returns|pages?|"
    r"/\s*(?:day|hour|min|sec)|per\s+(?:day|hour|minute|second|week|month))", re.I)
_NONE = re.compile(r"^\W*(none|n/?a|nil)\W*$", re.I)
_DONE_STATES = {"closed", "done", "resolved", "decided", "answered"}


@dataclass
class Ticket:
    kind: str
    title: str
    fields: Dict[str, str] = field(default_factory=dict)
    line: int = 0

    def get(self, name: str) -> str:
        return self.fields.get(name.lower(), "").strip()


def parse_tickets(text: str) -> List[Ticket]:
    tickets: List[Ticket] = []
    cur: Optional[Ticket] = None
    label: Optional[str] = None
    for n, line in enumerate(text.splitlines(), 1):
        h = _HEADER.match(line)
        if h:
            kind = h.group(1).upper()
            kind = "DEP" if kind.startswith("DEP") else kind
            cur = Ticket(kind, h.group(2), {}, n)
            tickets.append(cur)
            label = None
            continue
        if cur is None:
            continue
        m = _LABEL_LINE.match(line)
        if m:
            label = m.group(1).lower()
            cur.fields[label] = m.group(2)
        elif label and line.strip():
            cur.fields[label] += "\n" + line
    return tickets


_KIND_PREFIX = re.compile(r"^\s*(?:RISK|DEP(?:ENDENCY)?|DECISION)\s*:\s*", re.I)


def ref_key(title: str) -> str:
    """Pulse titles read 'DEP: what we need'; references may or may not repeat the prefix."""
    return normalize(_KIND_PREFIX.sub("", title))


def _first_date(text: str) -> Optional[date]:
    m = _DATE.search(text)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(0))
    except ValueError:
        return None


def _is_none(text: str) -> bool:
    return bool(_NONE.match(text.strip()))


def lint_tickets(text: str, rules: dict, stage: str = "draft", today: Optional[date] = None,
                 name: str = "ticket set") -> LintResult:
    """stage 'draft' = what AI produced; 'ready' = what the tech lead is about to accept (Gate 2)."""
    cfg = rules["lint"]
    c = Collector(name, cfg.get("severity_overrides"))
    today = today or date.today()
    tickets = parse_tickets(text)

    epics = [t for t in tickets if t.kind == "EPIC"]
    stories = [t for t in tickets if t.kind == "STORY"]
    links = [t for t in tickets if t.kind in ("DEP", "RISK", "DECISION")]

    if not tickets:
        c.add("TK000", BLOCK, "No tickets found. Use headings like '### STORY: <title>'.")
        return c.result
    if len(epics) != 1:
        c.add("TK012", BLOCK, f"Expected exactly one EPIC, found {len(epics)}.")
    if not stories:
        c.add("TK000", BLOCK, "No STORY tickets found.")

    blocker_titles = {ref_key(t.title): t for t in links}
    owner_date_gaps = 0
    systems = [s.lower() for s in cfg.get("systems", [])]
    verbs = set(cfg.get("verbs", []))

    for t in epics:
        where = f"EPIC: {t.title}"
        link_text = t.get("links")
        if not re.search(r"requirement pack|pack", link_text, re.I):
            c.add("TK012", BLOCK, "Epic must link the Requirement Pack.", where)
        if not t.get("why"):
            c.add("TK002", BLOCK, "Epic has no 'Why'.", where)

    for t in stories:
        where = f"STORY: {t.title}"
        words = t.title.split()
        if len(words) < 4:
            c.add("TK001", BLOCK, "Title too thin. Use verb + object + context, e.g. "
                                  "'Add returns export with date filter to Magento admin'.", where)
        elif normalize(words[0]) not in verbs:
            c.add("TK001", WARN, f"Title should start with a verb (found '{words[0]}').", where)

        why = t.get("why")
        if not why:
            c.add("TK002", BLOCK, "Missing 'Why' (two lines: the business reason, not the feature).", where)
        elif len(re.findall(r"[.!?](?:\s|$)", why)) > 3:
            c.add("TK002", WARN, "'Why' is longer than two lines. Keep the business reason short.", where)

        for lab in ("Scope in", "Scope out"):
            if not t.get(lab):
                c.add("TK003", BLOCK, f"Missing '{lab}'. Copy it from the pack.", where)

        ac = t.get("acceptance criteria")
        if not ac:
            c.add("TK004", BLOCK, "Missing acceptance criteria.", where)
        else:
            items, _ = numbered_items(ac)
            if not items:
                c.add("TK004", BLOCK, "Acceptance criteria must be numbered.", where)
            for i, it in enumerate(items, 1):
                if not _GWT.search(it):
                    c.add("TK004", BLOCK, f"Criterion {i} is not Given / When / Then.", where)

        tc = t.get("technical context")
        if not tc:
            c.add("TK005", BLOCK, "Missing technical context (systems, modules, volumes, client constraint).", where)
        else:
            low = tc.lower()
            if not any(re.search(r"(?<![a-z])" + re.escape(s) + r"(?![a-z])", low) for s in systems):
                c.add("TK005", BLOCK, "Technical context names no recognised system. Say 'Magento admin', "
                                      "'Pimcore asset tab', not 'the back end'.", where)
            if not _VOLUME.search(tc):
                c.add("TK005", WARN, "Technical context states no volume (rows, orders, users, per day).", where)

        lk = t.get("links")
        if not re.search(r"pack", lk, re.I):
            c.add("TK006", BLOCK, "Links must include the Requirement Pack.", where)
        if not re.search(r"granola|call", lk, re.I):
            c.add("TK006", WARN, "Links should include the Granola call.", where)
        if not re.search(r"epic", lk, re.I) and not t.get("epic"):
            c.add("TK006", WARN, "Links should name the parent epic.", where)

        if not t.get("client's words"):
            c.add("TK016", BLOCK, "Missing 'Client's words'. The ticket must carry the quotes it came from so it stands "
                                  "alone, without the pack or the call.", where)
        if not t.get("answers from the client"):
            c.add("TK016", WARN, "No 'Answers from the client'. Write 'None' if nothing was clarified.", where)

        if not t.get("assumptions"):
            c.add("TK007", WARN, "No 'Assumptions' field. Write 'None' if there are none.", where)

        blocked = t.get("blocked by")
        if not blocked:
            c.add("TK011", WARN, "No 'Blocked by' field. Write 'none' if nothing blocks this story.", where)
        elif not _is_none(blocked):
            for ref in [r.strip(" -*\t") for r in re.split(r"[\n;]", blocked) if r.strip(" -*\t")]:
                if ref_key(ref) not in blocker_titles:
                    c.add("TK011", BLOCK, f"Blocked by '{ref}', which is not a DEP/RISK/DECISION ticket "
                                          "in this set. If it is not a ticket, it is not managed.", where)

        if stage == "ready" and not t.get("size"):
            c.add("TK014", BLOCK, "No size from the tech lead. Not sized, not ready.", where)

    for t in links:
        where = f"{t.kind}: {t.title}"
        if t.kind == "DEP":
            need = {"owner": "Owner", "needed by": "Needed by", "what stops if late": "What stops if late",
                    "blocks": "Blocks"}
        elif t.kind == "RISK":
            need = {"impact": "Impact", "mitigation": "Mitigation", "owner": "Owner",
                    "review date": "Review date"}
        else:
            need = {"who decides": "Who decides", "options": "Options",
                    "cost of deciding late": "Cost of deciding late"}
        for key, label in need.items():
            if not t.get(key):
                c.add(f"TK0{'08' if t.kind == 'DEP' else '09' if t.kind == 'RISK' else '10'}", BLOCK,
                      f"Missing '{label}'.", where)
                if key in ("owner", "needed by", "review date"):
                    owner_date_gaps += 1
        for key in ("needed by", "review date"):
            v = t.get(key)
            if v:
                d = _first_date(v)
                if d is None:
                    c.add("TK013", BLOCK, f"'{key.title()}' needs an ISO date (YYYY-MM-DD).", where)
                    owner_date_gaps += 1
                elif d < today and stage == "draft":
                    c.add("TK013", WARN, f"'{key.title()}' {d} is already in the past.", where)
        if stage == "ready" and t.kind == "DECISION":
            status = normalize(t.get("status"))
            if status not in _DONE_STATES:
                c.add("TK015", BLOCK, "Decision still open. No open question that would change the build "
                                      "may remain at Gate 2.", where)

    c.result.stats = {
        "epics": len(epics), "stories": len(stories), "deps": sum(1 for t in links if t.kind == "DEP"),
        "risks": sum(1 for t in links if t.kind == "RISK"),
        "decisions": sum(1 for t in links if t.kind == "DECISION"),
        "owner_date_gaps": owner_date_gaps, "stage": stage,
    }
    return c.result
