"""Requirement Pack linter. The anti-hallucination gate in front of Gate 1.

The key rule: every scope item and acceptance criterion carries a [Q: "..."] tag, and the
quote must exist in the call transcript. Anything the client never said cannot be scope.
"""
from __future__ import annotations

import re
from typing import Dict, Optional

from .findings import BLOCK, INFO, WARN, Collector, LintResult
from .mdparse import (find_quote, normalize, numbered_items, plain_quotes, quotes_in,
                      split_sections, strip_tags, tag_values)

SECTION_SPECS = [
    ("summary", r"summary"),
    ("context", r"context"),
    ("scope_in", r"scope\s*[-–—:]*\s*in\b"),
    ("scope_out", r"scope\s*[-–—:]*\s*out\b"),
    ("acceptance", r"acceptance"),
    ("assumptions", r"assumption"),
    ("questions", r"open question"),
    ("risks", r"risk"),
    ("effort", r"effort"),
]
SECTION_LABEL = {
    "summary": "One-line summary", "context": "Context", "scope_in": "Scope: in",
    "scope_out": "Scope: out", "acceptance": "Acceptance criteria",
    "assumptions": "Assumptions", "questions": "Open questions",
    "risks": "Risks and dependencies", "effort": "Effort shape",
}
_GWT = re.compile(r"\bgiven\b.+?\bwhen\b.+?\bthen\b", re.I | re.S)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DAYS = re.compile(r"\d+(?:\s*[-–]\s*\d+)?\s*(?:person[- ])?(?:days?|d\b|weeks?|hours?|hrs?)", re.I)
_NONE = re.compile(r"^\W*(none|n/?a|nil)\W*$", re.I)


def _match_sections(text: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for heading, body in split_sections(text, 2):
        h = re.sub(r"^\s*\d+[.)]?\s*", "", heading).lower()
        for key, pat in SECTION_SPECS:
            if key not in found and re.search(pat, h):
                found[key] = body
                break
    return found


def _contains_term(text: str, term: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(term.lower()) + r"(?![a-z])", text.lower()) is not None


def lint_pack(text: str, transcript: Optional[str], rules: dict,
              name: str = "requirement pack") -> LintResult:
    cfg = rules["lint"]
    c = Collector(name, cfg.get("severity_overrides"))
    sections = _match_sections(text)

    meta = text.split("\n## ", 1)[0]
    if not re.search(r"Project:\s*[A-Z][A-Z0-9]{1,9}\b", meta):
        c.add("PK018", WARN, "Header has no 'Project: <Pulse key>' line, so the pack cannot be linked to its tickets.")
    if not re.search(r"Call:\s*\S+", meta):
        c.add("PK018", WARN, "Header has no 'Call: <Granola note title>' line, so the source call is not traceable.")

    for key, _ in SECTION_SPECS:
        if key not in sections:
            c.add("PK001", BLOCK, f"Missing section '{SECTION_LABEL[key]}'.")
        elif not sections[key].strip():
            c.add("PK002", BLOCK, f"Section '{SECTION_LABEL[key]}' is empty.", SECTION_LABEL[key])

    t_norm = t_words = None
    if transcript:
        t_norm = normalize(transcript)
        t_words = t_norm.split()
    else:
        c.add("PK006", WARN, "No transcript supplied, so quotes could not be verified against the call.")

    thr = cfg.get("quote_fuzzy_threshold", 0.88)
    min_words = cfg.get("quote_min_words", 3)

    def verify_quote(q: str, where: str) -> None:
        if len(normalize(q).split()) < min_words:
            c.add("PK006", WARN, f"Quote \"{q}\" is too short to be evidence.", where)
        if not transcript:
            return
        status, score = find_quote(q, t_norm, t_words)
        if status == "exact":
            return
        if status == "close" and score >= thr:
            c.add("PK006", WARN, f"Quote \"{q}\" is a paraphrase, not the client's exact words "
                                 f"(match {score:.0%}). Quote them verbatim.", where)
        else:
            c.add("PK006", BLOCK, f"Quote \"{q}\" does not appear in the transcript "
                                  f"(best match {score:.0%}). The AI may have invented it.", where)

    # 1 summary
    if "summary" in sections and sections["summary"].strip():
        qs = plain_quotes(sections["summary"])
        if not qs:
            c.add("PK003", BLOCK, "Summary has no quoted client words. State the job in their words.", "Summary")
        for q in qs:
            verify_quote(q, "Summary")

    # 3 scope in
    scope_items = []
    if sections.get("scope_in", "").strip():
        scope_items, bullets = numbered_items(sections["scope_in"])
        if not scope_items:
            c.add("PK004", BLOCK, "Scope in must be a numbered list.", "Scope: in")
        elif bullets:
            c.add("PK004", WARN, "Some scope items are bullets, not numbered.", "Scope: in")
        for i, item in enumerate(scope_items, 1):
            qs = quotes_in(item)
            if not qs:
                c.add("PK005", BLOCK, f"Scope item {i} has no [Q: \"...\"] source. If the client did not say it, "
                                      "it belongs in Assumptions or Open questions, not Scope.",
                      f"Scope in #{i}")
            for q in qs:
                verify_quote(q, f"Scope in #{i}")

    # 4 scope out
    if sections.get("scope_out", "").strip() and not numbered_items(sections["scope_out"])[0] \
            and not _NONE.match(sections["scope_out"].strip()):
        c.add("PK004", WARN, "Scope out should be a numbered list (or an explicit 'None').", "Scope: out")

    # 5 acceptance criteria
    ac_items = []
    if sections.get("acceptance", "").strip():
        ac_items, bullets = numbered_items(sections["acceptance"])
        if not ac_items:
            c.add("PK004", BLOCK, "Acceptance criteria must be a numbered list.", "Acceptance criteria")
        elif bullets:
            c.add("PK004", WARN, "Some criteria are bullets, not numbered.", "Acceptance criteria")
        for i, item in enumerate(ac_items, 1):
            body = strip_tags(item)
            where = f"Acceptance #{i}"
            if not _GWT.search(body):
                c.add("PK007", BLOCK, "Not in Given / When / Then form.", where)
            for term in cfg.get("vague_terms", []):
                if _contains_term(body, term):
                    c.add("PK008", BLOCK, f"Untestable wording '{term}'. Say what a person can observe.", where)
                    break
            qs = quotes_in(item)
            if not qs:
                c.add("PK009", BLOCK, "Criterion has no [Q: \"...\"] source from the call.", where)
            for q in qs:
                verify_quote(q, where)
        if scope_items and len(ac_items) < len(scope_items):
            c.add("PK010", WARN, f"{len(scope_items)} scope items but only {len(ac_items)} criteria. "
                                 "Each scope item needs at least one.", "Acceptance criteria")

    # 6 assumptions
    if sections.get("assumptions", "").strip():
        items, _ = numbered_items(sections["assumptions"])
        if not items and not _NONE.match(sections["assumptions"].strip()):
            c.add("PK004", WARN, "Assumptions should be a numbered list (or an explicit 'None').", "Assumptions")
        elif not items:
            c.add("PK011", WARN, "No assumptions listed. A call almost never states everything. Check the AI "
                                 "did not slip assumptions into Scope.", "Assumptions")

    # 7 open questions
    oq_count = 0
    if sections.get("questions", "").strip():
        items, _ = numbered_items(sections["questions"])
        oq_count = len(items)
        if not items and not _NONE.match(sections["questions"].strip()):
            c.add("PK004", BLOCK, "Open questions must be a numbered list.", "Open questions")
        elif not items:
            c.add("PK012", WARN, "No open questions. A call always leaves some ambiguity; re-read the transcript.",
                  "Open questions")
        for i, item in enumerate(items, 1):
            if not tag_values(item, "Owner"):
                c.add("PK013", BLOCK, "Open question has no [Owner: ...]. Who must answer it?", f"Open question #{i}")

    # 8 risks and dependencies
    rd_count = 0
    if sections.get("risks", "").strip():
        items, _ = numbered_items(sections["risks"])
        rd_count = len(items)
        for i, item in enumerate(items, 1):
            where = f"Risk/dep #{i}"
            if not re.match(r"\s*(RISK|DEP|DEPENDENCY|DECISION)\b", item, re.I):
                c.add("PK014", BLOCK, "Start each item with RISK:, DEP: or DECISION: so it becomes the right ticket.", where)
            if not tag_values(item, "Owner"):
                c.add("PK014", BLOCK, "Missing [Owner: ...].", where)
            dates = tag_values(item, "By")
            if not dates or not _DATE.fullmatch(dates[0]):
                c.add("PK014", BLOCK, "Missing [By: YYYY-MM-DD] date.", where)

    # 9 effort shape
    if sections.get("effort", "").strip():
        body = sections["effort"]
        if not _DAYS.search(body):
            c.add("PK015", WARN, "Effort shape gives no size in days.", "Effort shape")
        for term in cfg.get("commitment_terms", []):
            if _contains_term(body, term):
                c.add("PK015", BLOCK, f"Effort shape uses commitment language ('{term}'). It is a shape, "
                                      "not a commitment.", "Effort shape")
                break

    if transcript:
        _coverage(c, transcript, cfg)

    c.result.stats = {
        "scope_items": len(scope_items), "criteria": len(ac_items),
        "open_questions": oq_count, "risks_and_deps": rd_count,
    }
    return c.result


COVERAGE_LABELS = {
    "job_to_be_done": "the job to be done in the client's words",
    "who_and_how_often": "who does it and how often (volumes, peaks)",
    "what_happens_today": "what happens today, step by step",
    "what_done_looks_like": "what done looks like to the client",
    "boundaries": "the boundaries (systems, data, sign-off)",
    "date_and_reason": "the date and what hangs on it",
}


def topic_coverage(text: str, cfg: dict) -> Dict[str, Optional[str]]:
    """For each of the six must-haves: the keyword that matched, or None. A keyword heuristic, so it errs towards
    'not found' (a false 'found' would hide a real gap)."""
    t = text.lower()
    out: Dict[str, Optional[str]] = {}
    for key, words in cfg.get("coverage_topics", {}).items():
        if key.startswith("_"):
            continue
        out[key] = next((w for w in words if _keyword_hit(t, w)), None)
    return out


def _keyword_hit(text: str, kw: str) -> bool:
    if kw.startswith("re:"):
        return re.search(kw[3:], text) is not None
    return re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])", text) is not None


def assess_call(text: str, rules: dict) -> dict:
    cfg = rules["lint"]
    cov = topic_coverage(text, cfg)
    covered = [k for k, v in cov.items() if v]
    missing = [k for k, v in cov.items() if not v]
    need = cfg.get("call_ready_min_topics", 5)
    return {"covered": covered, "missing": missing, "needed": need, "ready": len(covered) >= need,
            "words": len(text.split())}


def _coverage(c: Collector, transcript: str, cfg: dict) -> None:
    """Advisory: did the call cover the six must-haves? Keyword heuristic, so warn only."""
    for key, hit in topic_coverage(transcript, cfg).items():
        if not hit:
            c.add("PK017", WARN, f"Transcript may not cover {COVERAGE_LABELS.get(key, key)}. If it was not discussed, "
                                 "this is a follow-up call, not a requirement.")
