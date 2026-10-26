"""Turns GitHub and Slack webhooks into events. The reference implementation for the tech team: signature checks are
real, payload mapping is tested against GitHub's payload shapes, and nothing here talks outbound.

GitHub -> events: pr.opened, pr.pushed, pr.reviewed, pr.merged, parakh.verdict.
A pull request is tied to its requirement through the Pulse ticket key in its branch name (GitHub flow naming). One that
names no ticket of any flow is logged under '<KEY>-UNLINKED' so governance sees it instead of losing it.

Slack: signature verification and the url_verification handshake are done; event mapping is left as a documented stub
(docs/ADAPTERS.md) because it needs the Slack app and the gate-message store that the tech team will build.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Dict, List, Optional

from .project import branch_problem

_BRANCH = re.compile(r"^(?:feature|bugfix|hotfix)/([A-Z][A-Z0-9]{1,9}-\d+)-")
_PLACEHOLDER = re.compile(r"^\W*(|n/?a|none|tbd|todo|<.*>)\W*$", re.I)
_FOOTER = re.compile(r"<!--\s*parakh:\s*(\{.*?\})\s*-->", re.S)
SENIOR_ROLES = ("tech_lead", "senior", "delegate")


# ---------- signatures ----------

def verify_github(secret: str, body: bytes, header: Optional[str]) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def verify_slack(signing_secret: str, timestamp: Optional[str], body: bytes, signature: Optional[str],
                 now: Optional[float] = None, max_age: int = 300) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        age = abs((now if now is not None else time.time()) - float(timestamp))
    except ValueError:
        return False
    if age > max_age:                     # a replayed request
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------- reading a pull request ----------

def _field(body: str, label: str) -> str:
    m = re.search(rf"^\s*{label}\s*:[ \t]*(.*)$", body or "", re.I | re.M)
    return m.group(1).strip() if m else ""


def parse_pr_body(body: str) -> dict:
    ai = _field(body, "AI-assisted").lower()
    footer = None
    m = _FOOTER.search(body or "")
    if m:
        try:
            footer = json.loads(m.group(1))
            if not (isinstance(footer, dict) and re.fullmatch(r"[0-9a-fA-F]{7,40}", str(footer.get("head_sha", "")))
                    and str(footer.get("gate", "")).lower() in ("pass", "fail")):
                footer = None      # unfilled placeholders (<pass or fail>, <full commit sha>) are not a verdict
        except json.JSONDecodeError:
            footer = None
    explain = re.search(r"I can explain every line\s*:\s*\[\s*[xX]\s*\]", body or "")
    return {
        "ticket": (re.search(r"^\s*Ticket\s*:\s*([A-Z][A-Z0-9]{1,9}-\d+)", body or "", re.I | re.M) or [None, ""])[1].upper(),
        "tier": (re.search(r"^\s*Tier\s*:\s*(T[123])\b", body or "", re.I | re.M) or [None, ""])[1].upper(),
        "ai_declared": ai.startswith(("yes", "no")),
        "explains_every_line": bool(explain),
        "failure_path_test": _field(body, "Failure-path test") if not _PLACEHOLDER.match(_field(body, "Failure-path test")) else "",
        "rollback_note": _field(body, "Rollback note") if not _PLACEHOLDER.match(_field(body, "Rollback note")) else "",
        "parakh": footer,
    }


def _flow_for_key(store, key: str) -> Optional[str]:
    for e in store.events():
        if e.type == "pulse.mapped" and e.data["key"] == key and re.match(r"^s\d+$", e.data["local_id"]):
            return e.flow
        if e.type == "ticket.created" and e.data["ticket"] == key:
            return e.flow
    return None


def _known_keys(store) -> set:
    return {e.data["key"] for e in store.events(type="pulse.mapped") if re.match(r"^s\d+$", e.data["local_id"])} | \
           {e.data["ticket"] for e in store.events(type="ticket.created")}


def _who(cfg: dict, login: str):
    for role, p in (cfg.get("people") or {}).items():
        if p.get("github", "").lower() == (login or "").lower():
            return role, p
    return None, None


def _already(store, flow: str, delivery: str) -> bool:
    return bool(delivery) and any(e.data.get("delivery") == delivery for e in store.events(flow=flow))


# ---------- GitHub ----------

def process_github(event: str, payload: dict, cfg: dict, store, delivery: str = "") -> dict:
    repo = (payload.get("repository") or {}).get("full_name", "")
    if repo.lower() != (cfg["git"].get("repo") or "").lower():
        return {"handled": False, "note": f"repository '{repo}' is not this project's repository"}
    key = cfg["key"]
    if event == "pull_request":
        return _pull_request(payload, cfg, store, key, delivery)
    if event == "pull_request_review":
        return _review(payload, cfg, store, key, delivery)
    if event == "issue_comment":
        return _comment(payload, cfg, store, key, delivery)
    return {"handled": False, "note": f"event '{event}' is not used"}


def _pr_flow(pr: dict, store, key: str):
    ref = pr["head"]["ref"]
    m = _BRANCH.match(ref)
    ticket = m.group(1) if m else ""
    flow = _flow_for_key(store, ticket) if ticket else None
    return (flow or f"{key}-UNLINKED"), ticket


def _log(store, type_, flow, key, actor, data, delivery):
    if delivery:
        data = {**data, "delivery": delivery}
    store.append(type_, flow, key, actor, data, track="build")
    return type_


def _verdict(store, flow, key, pr_no, body_info, actor, delivery, head_sha=None) -> List[str]:
    f = body_info["parakh"]
    if not f:
        return []
    dup = any(e.data.get("pr") == pr_no and e.data.get("head_sha") == f["head_sha"] and str(e.data.get("gate")) == str(f["gate"])
              for e in store.events(flow=flow, type="parakh.verdict"))
    if dup:
        return []
    data = {"pr": pr_no, "head_sha": f["head_sha"], "gate": str(f["gate"]).lower(),
            "critical": int(f.get("critical", 0)), "major": int(f.get("major", 0))}
    return [_log(store, "parakh.verdict", flow, key, actor, data, delivery)]


def _pull_request(payload, cfg, store, key, delivery) -> dict:
    action, pr = payload.get("action"), payload["pull_request"]
    no, author, sha = payload.get("number") or pr["number"], pr["user"]["login"], pr["head"]["sha"]
    flow, ticket = _pr_flow(pr, store, key)
    if _already(store, flow, delivery):
        return {"handled": True, "flow": flow, "events": [], "note": "duplicate delivery ignored"}
    info = parse_pr_body(pr.get("body") or "")
    logged: List[str] = []
    if action in ("opened", "reopened", "ready_for_review"):
        ticket_decl = info["ticket"] or ticket
        problem = branch_problem(cfg, pr["head"]["ref"], _known_keys(store))
        if pr["base"]["ref"] != cfg["git"]["base_branch"]:
            problem = (problem + "; " if problem else "") + f"pull request targets '{pr['base']['ref']}', not '{cfg['git']['base_branch']}'"
        logged.append(_log(store, "pr.opened", flow, key, author, {
            "pr": no, "author": author, "head_sha": sha, "ticket": ticket_decl if ticket_decl == ticket or not ticket else "",
            "ai_declared": info["ai_declared"], "tier": info["tier"], "failure_path_test": info["failure_path_test"],
            "rollback_note": info["rollback_note"], "explains_every_line": info["explains_every_line"],
            "branch_problem": problem or "", "base": pr["base"]["ref"], "repo": cfg["git"]["repo"]}, delivery))
        logged += _verdict(store, flow, key, no, info, author, "")
    elif action == "synchronize":
        logged.append(_log(store, "pr.pushed", flow, key, author, {"pr": no, "head_sha": sha}, delivery))
        logged += _verdict(store, flow, key, no, info, author, "")
    elif action == "edited":
        logged += _verdict(store, flow, key, no, info, author, delivery)
    elif action == "closed" and pr.get("merged"):
        reviews = {}
        for e in store.events(flow=flow, type="pr.reviewed"):
            if e.data["pr"] == no and (not e.data.get("head_sha") or e.data["head_sha"] == sha):
                reviews[e.data["reviewer"].lower()] = e.data["state"]
        approvers = sorted(r for r, st in reviews.items() if st == "approved")
        merger = (pr.get("merged_by") or {}).get("login", author)
        mrole, _ = _who(cfg, merger)
        senior = any(_who(cfg, a)[0] in SENIOR_ROLES for a in approvers)
        logged.append(_log(store, "pr.merged", flow, key, merger, {
            "pr": no, "merger": merger, "merger_role": "tech_lead" if mrole in ("tech_lead", "delegate") else (mrole or "developer"),
            "approvers": approvers, "senior_approved": senior}, delivery))
    else:
        return {"handled": False, "note": f"pull_request action '{action}' is not used"}
    return {"handled": True, "flow": flow, "events": logged}


def _review(payload, cfg, store, key, delivery) -> dict:
    if payload.get("action") != "submitted":
        return {"handled": False, "note": "only submitted reviews are used"}
    pr, rv = payload["pull_request"], payload["review"]
    flow, _ = _pr_flow(pr, store, key)
    if _already(store, flow, delivery):
        return {"handled": True, "flow": flow, "events": [], "note": "duplicate delivery ignored"}
    state = rv["state"].lower()
    if state not in ("approved", "changes_requested", "commented", "dismissed"):
        return {"handled": False, "note": f"review state '{state}' is not used"}
    e = _log(store, "pr.reviewed", flow, key, rv["user"]["login"],
             {"pr": pr["number"], "reviewer": rv["user"]["login"], "state": state, "head_sha": rv.get("commit_id", "")}, delivery)
    return {"handled": True, "flow": flow, "events": [e]}


def _comment(payload, cfg, store, key, delivery) -> dict:
    issue = payload.get("issue") or {}
    if "pull_request" not in issue or payload.get("action") != "created":
        return {"handled": False, "note": "not a new pull request comment"}
    info = parse_pr_body((payload.get("comment") or {}).get("body", ""))
    if not info["parakh"]:
        return {"handled": False, "note": "comment carries no Parakh footer"}
    # which flow? the PR's branch is not in this payload, so find it from the PR's earlier events
    no = issue["number"]
    flow = next((e.flow for e in store.events(project=key, type="pr.opened") if e.data["pr"] == no), f"{key}-UNLINKED")
    logged = _verdict(store, flow, key, no, info, payload["comment"]["user"]["login"], delivery)
    return {"handled": True, "flow": flow, "events": logged}


# ---------- Slack ----------

def process_slack(payload: dict) -> dict:
    """url_verification is answered. Everything else is recorded raw for the tech team to map (see docs/ADAPTERS.md)."""
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    return {"stub": True, "type": (payload.get("event") or {}).get("type", payload.get("type"))}
