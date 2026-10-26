"""Paste mode: transcripts arrive by being dropped into data/<KEY>/inbox/. No connector, no tokens.

The rules are deliberate and not configurable:
  * only a transcript is a source. Text that looks like an AI summary is refused, because quotes checked against a
    paraphrase prove only that the words are in the paraphrase.
  * a paste that is too short to be a call is refused.
  * the same call pasted twice is recognised and filed away, not drafted twice.
  * a note tagged for another project is refused, so a call never lands on the wrong board.

A refused file stays where it is, with the reason, until someone fixes or removes it.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple

from .mdparse import normalize

MIN_WORDS = 100
_BULLET = re.compile(r"^\s*([-*•]|\d+[.)])\s")
_SPEAKER = re.compile(r"^[A-Z][\w .()'-]{0,40}:\s")


def split_paste(text: str) -> Tuple[Optional[str], str, Dict[str, str]]:
    """Reads the header Granola adds when a transcript is copied, or a simple 'Title:' first line.
    Returns (title, body, meta). The body never includes the header."""
    lines = text.splitlines()
    head = lines[:25]
    meta: Dict[str, str] = {}
    title = None
    for line in head:
        fm = re.match(r"^\s*(?:Flow|Attach to|Attach)\s*:\s*([A-Z][A-Z0-9]{1,9}-(?:REQ|SUP)-\d+)\s*$", line, re.I)
        if fm:
            meta["flow"] = fm.group(1).upper()
        m = re.match(r"^\s*(Meeting Title|Title|Date|Meeting participants|Participants)\s*:\s*(.+?)\s*$", line, re.I)
        if m:
            k = m.group(1).lower()
            if k in ("meeting title", "title") and title is None:
                title = m.group(2)
            elif k == "date":
                meta["date"] = m.group(2)
            elif "participants" in k:
                meta["participants"] = m.group(2)
    idx = next((i for i, l in enumerate(head) if re.match(r"^\s*Transcript\s*:\s*(.*)$", l, re.I)), None)
    if idx is not None:
        first = re.match(r"^\s*Transcript\s*:\s*(.*)$", head[idx], re.I).group(1)
        body = "\n".join(([first] if first else []) + lines[idx + 1:])
    elif lines and re.match(r"^\s*(Meeting Title|Title|Flow|Attach to|Attach)\s*:", lines[0], re.I):
        k = 0
        while k < len(lines) and re.match(r"^\s*(Meeting Title|Title|Flow|Attach to|Attach|Date|Meeting participants|Participants)\s*:", lines[k], re.I):
            k += 1
        body = "\n".join(lines[k:])
    else:
        body = text
    return title, body.strip(), meta


def looks_like_summary(text: str) -> Optional[str]:
    """Returns why the text looks like an AI summary rather than a transcript, or None."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return "it is empty"
    bullets = sum(1 for l in lines if _BULLET.match(l))
    headings = sum(1 for l in lines if l.lstrip().startswith("#"))
    speakers = sum(1 for l in lines if _SPEAKER.match(l))
    if bullets / len(lines) >= 0.5 and speakers < 3:
        return "most lines are bullet points and nobody is speaking, which is how a summary looks"
    if headings >= 2 and bullets >= 3 and speakers < 3:
        return "it has section headings and bullets but no speaker turns, which is how a summary looks"
    return None


def source_problem(body: str) -> Optional[str]:
    words = len(body.split())
    if words < MIN_WORDS:
        return f"only {words} words: too short to be a call transcript (minimum {MIN_WORDS})"
    why = looks_like_summary(body)
    if why:
        return f"this looks like an AI summary, not a transcript ({why}). Paste the transcript."
    return None


def content_ref(body: str) -> str:
    return "paste:" + hashlib.sha256(normalize(body).encode("utf-8")).hexdigest()[:16]


def _park(src: Path, dest_dir: Path, name: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{dest.stem}.{n}{dest.suffix}"
        n += 1
    shutil.move(str(src), str(dest))
    return dest


def _attach_followup(cfg: dict, data_dir: Path, store, flow: str, ref: str, title: str, body: str, meta: dict):
    """A follow-up call joins the flow it belongs to: its transcript is appended, the call is assessed again, and if a
    pack already exists the pack is reopened so the revision, gates and tickets are redone against it."""
    key = cfg["key"]
    evs = store.events(flow=flow)
    if not any(e.type == "call.recorded" for e in evs):
        return {"status": "unknown_flow", "detail": f"there is no call recorded for {flow}"}
    if any(e.data.get("source_ref") == ref for e in store.events(project=key) if e.type in ("call.recorded", "call.followup")):
        return {"status": "exists", "flow": flow}
    tpath = data_dir / key / flow / "transcript.txt"
    if not tpath.exists():
        return {"status": "unknown_flow", "detail": f"{flow} has no transcript.txt to add to"}
    label = f"{title} ({meta['date']})" if meta.get("date") else title
    tpath.write_text(tpath.read_text(encoding="utf-8").rstrip() + f"\n\n=== FOLLOW-UP CALL: {label} ===\n\n" + body + "\n",
                     encoding="utf-8")
    store.append("call.followup", flow, key, "dcp", {"title": title, "source_ref": ref, **{k: v for k, v in meta.items() if k != "flow"}},
                 track="build")
    packs = [e for e in evs if e.type == "pack.drafted"]
    if packs:
        store.append("pack.reopened", flow, key, "dcp", {"reason": f"follow-up call: {title}",
                     "from_version": max(p.data["version"] for p in packs)}, track="build")
    return {"status": "attached", "flow": flow, "reopened": bool(packs)}


def ingest_inbox(cfg: dict, data_dir: Path, register_flow, store=None) -> dict:
    """register_flow(cfg, ref, title, meta) -> (code, dict). Passed in so this module stays free of the CLI."""
    key = cfg["key"]
    inbox = data_dir / key / "inbox"
    res = {"inbox": str(inbox), "ingested": [], "duplicates": [], "rejected": [], "needs_rename": [], "attached": []}
    if cfg.get("track") != "build":
        return res
    inbox.mkdir(parents=True, exist_ok=True)
    for f in sorted(p for p in inbox.iterdir()
                    if p.is_file() and p.suffix.lower() in (".txt", ".md") and not p.name.startswith(("_", "."))):
        text = f.read_text(encoding="utf-8", errors="replace")
        title, body, meta = split_paste(text)
        title = title or f.stem
        problem = source_problem(body)
        if problem:
            res["rejected"].append({"file": f.name, "reason": problem})
            continue
        if meta.get("flow") and store is not None:
            out = _attach_followup(cfg, data_dir, store, meta["flow"], content_ref(body), title, body, meta)
            if out["status"] == "unknown_flow":
                res["rejected"].append({"file": f.name, "reason": out["detail"]})
            elif out["status"] == "exists":
                _park(f, inbox / "processed", f"duplicate__{out['flow']}__{f.name}")
                res["duplicates"].append({"file": f.name, "flow": out["flow"]})
            else:
                _park(f, inbox / "processed", f"{out['flow']}__followup__{f.name}")
                res["attached"].append({"file": f.name, "flow": out["flow"], "reopened": out["reopened"], "words": len(body.split())})
            continue
        code, out = register_flow(cfg, content_ref(body), title, meta)
        if out["status"] == "wrong_project":
            res["rejected"].append({"file": f.name, "reason": out["detail"]})
            continue
        if out["status"] == "exists":
            _park(f, inbox / "processed", f"duplicate__{out['flow']}__{f.name}")
            res["duplicates"].append({"file": f.name, "flow": out["flow"]})
            continue
        flow_dir = data_dir / key / out["flow"]
        flow_dir.mkdir(parents=True, exist_ok=True)
        (flow_dir / "transcript.txt").write_text(body + "\n", encoding="utf-8")
        _park(f, inbox / "processed", f"{out['flow']}__{f.name}")
        res["ingested"].append({"file": f.name, "flow": out["flow"], "title": title, "words": len(body.split())})
        if out["status"] == "needs_rename":
            res["needs_rename"].append({"flow": out["flow"], "title": title})
    return res
