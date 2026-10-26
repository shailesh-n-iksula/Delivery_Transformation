"""Pack vs ticket-set drift. Catches scope silently added or dropped between Gate 1 and Gate 2,
and later when someone edits a Pulse ticket by hand."""
from __future__ import annotations

import re
from typing import List

from .findings import BLOCK, INFO, WARN, Collector, LintResult
from .mdparse import numbered_items, similarity, split_sections, strip_tags
from .packlint import _match_sections
from .ticketlint import parse_tickets


_GWT_SPLIT = re.compile(r"\bgiven\b(.+?)\bwhen\b(.+?)\bthen\b(.+)", re.I | re.S)


def _criterion_similarity(a: str, b: str) -> float:
    """Compare Given, When and Then separately and take the weakest. A rewritten outcome
    (Then) must not hide behind an identical set-up (Given/When)."""
    ma, mb = _GWT_SPLIT.search(a), _GWT_SPLIT.search(b)
    if ma and mb:
        return min(similarity(x, y) for x, y in zip(ma.groups(), mb.groups()))
    return similarity(a, b)


def _best(text: str, candidates: List[str], measure=similarity):
    best, idx = 0.0, -1
    for i, cand in enumerate(candidates):
        s = measure(text, cand)
        if s > best:
            best, idx = s, i
    return best, idx


def check_drift(pack_text: str, tickets_text: str, rules: dict, name: str = "pack vs tickets") -> LintResult:
    cfg = rules["lint"]
    sim = cfg.get("drift_similarity", {})
    t_ac, t_title, t_scope = sim.get("criterion", 0.75), sim.get("risk_or_dep_title", 0.6), sim.get("scope_to_story", 0.4)
    c = Collector(name, cfg.get("severity_overrides"))

    sections = _match_sections(pack_text)
    pack_ac = [strip_tags(i) for i in numbered_items(sections.get("acceptance", ""))[0]]
    pack_scope = [strip_tags(i) for i in numbered_items(sections.get("scope_in", ""))[0]]
    pack_rd = [strip_tags(i) for i in numbered_items(sections.get("risks", ""))[0]]
    pack_oq = numbered_items(sections.get("questions", ""))[0]

    tickets = parse_tickets(tickets_text)
    stories = [t for t in tickets if t.kind == "STORY"]
    links = [t for t in tickets if t.kind in ("DEP", "RISK", "DECISION")]

    ticket_ac: List[str] = []
    for s in stories:
        for it in numbered_items(s.get("acceptance criteria"))[0]:
            ticket_ac.append(strip_tags(it))

    for i, ac in enumerate(pack_ac, 1):
        score, _ = _best(ac, ticket_ac, _criterion_similarity)
        if score < t_ac:
            c.add("DR001", BLOCK, f"Pack criterion {i} is not in any story ('{ac[:70]}...'). Dropped scope.",
                  f"Pack acceptance #{i}")
    for s in stories:
        for j, it in enumerate(numbered_items(s.get("acceptance criteria"))[0], 1):
            score, _ = _best(strip_tags(it), pack_ac, _criterion_similarity)
            if score < t_ac:
                c.add("DR002", BLOCK, f"Criterion {j} is not in the confirmed pack ('{strip_tags(it)[:70]}...'). "
                                      "Scope added after the client confirmed.", f"STORY: {s.title}")

    link_titles = [t.title for t in links]
    for i, rd in enumerate(pack_rd, 1):
        body = re.sub(r"^\s*(RISK|DEP|DEPENDENCY|DECISION)\s*:?\s*", "", rd, flags=re.I)
        score, _ = _best(body, link_titles)
        if score < t_title:
            c.add("DR003", BLOCK, f"Pack risk/dependency {i} has no ticket ('{body[:70]}...'). "
                                  "If it is not a ticket, it is not managed.", f"Pack risks #{i}")
    pack_rd_bodies = [re.sub(r"^\s*(RISK|DEP|DEPENDENCY|DECISION)\s*:?\s*", "", r, flags=re.I) for r in pack_rd]
    pack_oq_bodies = [strip_tags(q) for q in pack_oq]
    for t in links:
        s1, _ = _best(t.title, pack_rd_bodies)
        s2, _ = _best(t.title, pack_oq_bodies)
        if max(s1, s2) < t_title:
            c.add("DR004", WARN, "Ticket is not in the pack. Either add it to the pack or remove the ticket.",
                  f"{t.kind}: {t.title}")

    story_text = [f"{s.title}. {s.get('why')}" for s in stories]
    for i, item in enumerate(pack_scope, 1):
        score, _ = _best(item, story_text)
        if score < t_scope:
            c.add("DR005", WARN, f"Scope item {i} has no matching story ('{item[:70]}...').", f"Pack scope #{i}")

    decisions = [t for t in links if t.kind == "DECISION"]
    if len(pack_oq) > len(decisions):
        c.add("DR006", INFO, f"{len(pack_oq)} open questions in the pack, {len(decisions)} DECISION tickets. "
                             "Only questions that block the build need tickets; confirm the rest went to the client.")

    c.result.stats = {"pack_criteria": len(pack_ac), "ticket_criteria": len(ticket_ac)}
    return c.result
