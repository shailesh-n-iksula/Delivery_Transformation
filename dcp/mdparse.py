from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import List, Tuple

_PUNCT = re.compile(r"[^a-z0-9\s]")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^\s*(\d+)[.)]\s+(.*\S)\s*$")
_BULLET = re.compile(r"^\s*[-*\u2022]\s")
_QUOTE_TAG = re.compile(r"\[Q:\s*[\"\u201c\u201d](.+?)[\"\u201c\u201d]\s*\]", re.S)
_ANY_TAG = re.compile(r"\[(?:Q|Owner|By|Type)\s*:[^\]]*\]", re.I)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).lower()
    t = _PUNCT.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def split_sections(text: str, level: int = 2) -> List[Tuple[str, str]]:
    sections: list = []
    cur = None
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m and len(m.group(1)) == level:
            cur = [m.group(2), []]
            sections.append(cur)
        elif cur is not None:
            cur[1].append(line)
    return [(h, "\n".join(b).strip("\n")) for h, b in sections]


def numbered_items(body: str) -> Tuple[List[str], int]:
    """Returns (numbered items, count of bullet lines that should have been numbered)."""
    items: List[str] = []
    bullets = 0
    for line in body.splitlines():
        if not line.strip():
            continue
        m = _ITEM.match(line)
        if m:
            items.append(m.group(2))
        elif _BULLET.match(line):
            bullets += 1
        elif items:
            items[-1] += " " + line.strip()
    return items, bullets


def quotes_in(text: str) -> List[str]:
    return [q.strip() for q in _QUOTE_TAG.findall(text)]


def tag_values(text: str, name: str) -> List[str]:
    return [v.strip() for v in re.findall(r"\[%s\s*:\s*([^\]]*)\]" % name, text, re.I)]


def strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", _ANY_TAG.sub("", text)).strip()


def plain_quotes(text: str) -> List[str]:
    return [q.strip() for q in re.findall(r"[\"\u201c](.+?)[\"\u201d]", text, re.S)]


def similarity(a: str, b: str) -> float:
    """Max of sequence ratio and token containment, on normalised text."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    contain = len(ta & tb) / min(len(ta), len(tb))
    return max(ratio, contain * 0.95)


def find_quote(quote: str, transcript_norm: str, transcript_words: List[str]) -> Tuple[str, float]:
    """Locate a quote in a transcript. Returns ('exact'|'close'|'missing', score)."""
    nq = normalize(quote)
    qw = nq.split()
    if not qw:
        return "missing", 0.0
    if nq in transcript_norm:
        return "exact", 1.0

    n = len(qw)
    qset = set(qw)
    prefix = [0]
    for w in transcript_words:
        prefix.append(prefix[-1] + (1 if w in qset else 0))
    need = max(1, int(0.6 * n))
    best = 0.0
    for i in range(0, max(1, len(transcript_words) - n + 1)):
        if prefix[min(i + n, len(prefix) - 1)] - prefix[i] >= need:
            window = " ".join(transcript_words[i:i + n])
            best = max(best, SequenceMatcher(None, nq, window).ratio())
    return ("close" if best > 0 else "missing"), best
