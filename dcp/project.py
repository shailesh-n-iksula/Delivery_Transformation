"""Per-project registration: the few inputs a delivery or tech person gives once.

Everything else (flow ids, ticket structure, gates, nudges, briefs) derives from these. A project
that already has a Slack channel and a Pulse board (typical for support work) registers in a minute.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from .models import DEFAULT_ESCALATE_AFTER, DEFAULT_MODELS, DEFAULT_RUNNER, validate_models

TRACKS = ("build", "support")
MODES = ("warn", "enforce")

DEFAULT_SCHEDULES = {
    "granola_poll_min": 15,      # look for new call notes
    "slack_poll_min": 5,         # gate replies, sizes, branch, support intake
    "pulse_sweep_min": 1440,     # Definition-of-Ready sweep over the board
    "governance_refresh_min": 1440,
    "gate_nudge_hours": 4,       # waiting on a human this long -> nudge
    "escalate_after_nudges": 2,  # then escalate to the delegate
}

_PLACEHOLDER = re.compile(r"<[^>]*>|\bTODO\b|\bCHANGE_ME\b", re.I)
_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SLACK_USER = re.compile(r"^U[A-Z0-9]{6,}$")
_SLACK_CHAN = re.compile(r"^(#[a-z0-9][a-z0-9._-]*|[CG][A-Z0-9]{6,})$")
_BOARD = re.compile(r"^https://[^/\s]+/projects/([A-Za-z0-9]+)/board/?$")
_REF = re.compile(r"^[A-Za-z0-9._/-]+$")
PROTECTED = {"main", "master", "develop", "release", "production", "prod"}


class ProjectError(ValueError):
    pass


def safe_ref(name: str) -> bool:
    """A git ref that is safe to hand to a shell: no leading dash, no '..', no odd characters."""
    return bool(name) and bool(_REF.match(name)) and not name.startswith("-") and ".." not in name \
        and not name.endswith("/") and not name.endswith(".lock")


def key_from_board_url(url: str) -> Optional[str]:
    m = _BOARD.match((url or "").strip())
    return m.group(1).upper() if m else None


def parse_person(text: str) -> Dict[str, str]:
    """'Meera N:U01ABC2345:meera@iksula.com' -> {name, slack_id, email}"""
    parts = [p.strip() for p in text.split(":")]
    if len(parts) < 2:
        raise ProjectError(f"person '{text}' must look like 'Name:SLACKID[:email]'")
    return {"name": parts[0], "slack_id": parts[1], "email": parts[2] if len(parts) > 2 else ""}


def make_project(key: str, track: str, board_url: str, slack_channel: str, granola_folder: str = "",
                 git_repo: str = "", base_branch: str = "main", delivery_manager: str = "",
                 tech_lead: str = "", delegate: str = "", name: str = "", support_parent: str = "",
                 intake_channel: str = "", mode: str = "warn", granola_source: str = "paste") -> dict:
    cfg: dict = {
        "key": key.upper(),
        "name": name or key.upper(),
        "track": track,
        "mode": mode,
        "pulse": {"project_key": key.upper(), "board_url": board_url,
                  "support_parent": support_parent, "title_prefix": "[L2] :" if track == "support" else ""},
        "slack": {"channel": slack_channel, "intake_channel": intake_channel or (slack_channel if track == "support" else "")},
        "granola": {"source": granola_source, "folder": granola_folder, "title_tag": key.upper()},
        "git": {"repo": git_repo, "base_branch": base_branch, "strategy": "github-flow",
                "rules_file": "CLAUDE.md", "pattern_files": []},
        "validation": {"require_tech_lead": False},
        "storage": {"backend": "jsonl"},
        "models": dict(DEFAULT_MODELS),
        "escalate_after_failures": DEFAULT_ESCALATE_AFTER,
        "runner": dict(DEFAULT_RUNNER),
        "people": {},
        "schedules": dict(DEFAULT_SCHEDULES),
        "tier_default": "T2",
    }
    if delivery_manager:
        cfg["people"]["delivery_manager"] = parse_person(delivery_manager)
    if tech_lead:
        cfg["people"]["tech_lead"] = parse_person(tech_lead)
    if delegate:
        cfg["people"]["delegate"] = {**parse_person(delegate), "covers": ["tech_lead"]}
    return cfg


def validate_project(cfg: dict) -> List[str]:
    """Returns human-readable problems; an empty list means ready to run."""
    problems: List[str] = []

    def get(path: str):
        cur = cfg
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def need(path: str, why: str, pattern=None, hint: str = "") -> None:
        v = get(path)
        if v in (None, ""):
            problems.append(f"{path}: not set ({why})")
        elif isinstance(v, str) and _PLACEHOLDER.search(v):
            problems.append(f"{path}: still a placeholder ('{v}'). {why}")
        elif pattern is not None and isinstance(v, str) and not pattern.match(v):
            problems.append(f"{path}: '{v}' is not valid. {hint}")

    track = cfg.get("track")
    if track not in TRACKS:
        problems.append(f"track: must be one of {TRACKS}")
        return problems
    if cfg.get("mode", "warn") not in MODES:
        problems.append(f"mode: must be one of {MODES}")

    need("key", "short project code, e.g. SP", _KEY, "Use 2 to 10 capitals or digits, starting with a letter.")
    need("pulse.project_key", "the Pulse project key", _KEY, "Use the key shown in Pulse.")
    if get("pulse.project_key") and get("key") and cfg["pulse"]["project_key"] != cfg["key"]:
        problems.append("pulse.project_key: differs from key. Calls are routed by the key in the note title, so keep them equal.")
    need("pulse.board_url", "link to the Pulse board", _BOARD, "Expected https://<host>/projects/<KEY>/board")
    bu = get("pulse.board_url")
    if isinstance(bu, str) and _BOARD.match(bu) and key_from_board_url(bu) != get("pulse.project_key"):
        problems.append("pulse.board_url: the board URL is for a different project than pulse.project_key.")

    need("slack.channel", "channel for gates, nudges and results", _SLACK_CHAN, "Use #channel-name or the channel id (C...).")
    need("people.tech_lead.slack_id", "tech lead approves Gate 2 and Gate 3", _SLACK_USER, "Slack member id looks like U01ABC2345.")
    need("people.tech_lead.name", "tech lead name")

    if track == "build":
        need("people.delivery_manager.slack_id", "delivery manager approves Gate 1", _SLACK_USER, "Slack member id looks like U01ABC2345.")
        need("people.delivery_manager.name", "delivery manager name")
        source = get("granola.source") or "paste"
        if source not in ("paste", "connector"):
            problems.append(f"granola.source: '{source}' must be 'paste' or 'connector'")
        elif source == "connector":
            need("granola.folder", "Granola folder holding this project's call notes")
        need("granola.title_tag", "key that call notes end with", _KEY)
        need("git.repo", "GitHub repo as owner/name", _REPO, "Expected owner/name.")
    else:
        need("slack.intake_channel", "channel where support requests arrive", _SLACK_CHAN, "Use #channel-name or the channel id (C...).")
        if get("git.repo"):
            need("git.repo", "GitHub repo as owner/name", _REPO, "Expected owner/name.")

    if get("git.repo") or track == "build":
        base = get("git.base_branch") or ""
        if not safe_ref(base):
            problems.append(f"git.base_branch: '{base}' is not a safe branch name.")
        strategy = get("git.strategy") or "github-flow"
        if strategy != "github-flow":
            problems.append(f"git.strategy: '{strategy}' is not supported. Only 'github-flow' is.")

    tl, dm = get("people.tech_lead.slack_id"), get("people.delivery_manager.slack_id")
    if track == "build" and tl and dm and tl == dm:
        problems.append("people: delivery manager and tech lead are the same person. Gate 1 and Gate 2 need two people.")

    problems.extend(validate_models(cfg))
    if (get("storage.backend") or "jsonl") not in ("jsonl", "sqlite"):
        problems.append(f"storage.backend: '{get('storage.backend')}' must be 'jsonl' or 'sqlite'")

    sched = get("schedules") or {}
    for k, default in DEFAULT_SCHEDULES.items():
        v = sched.get(k, default)
        if not isinstance(v, int) or v < 1:
            problems.append(f"schedules.{k}: must be a whole number of at least 1 (got {v!r})")
    for k in ("granola_poll_min", "slack_poll_min"):
        v = sched.get(k, DEFAULT_SCHEDULES[k])
        if isinstance(v, int) and 0 < v < 5:
            problems.append(f"schedules.{k}: {v} minutes is below the 5 minute floor (connector rate limits).")
    return problems


BRANCH_TYPES = ("feature", "bugfix", "hotfix")


def slugify(text: str, limit: int = 36) -> str:
    """Lower-case words joined by '-', cut at a word boundary, never mid-word."""
    text = re.sub(r"^(epic|story|bug|task)\s*:\s*", "", text.strip(), flags=re.I)
    parts = [p for p in re.sub(r"[^a-z0-9]+", "-", text.lower()).split("-") if p]
    out = ""
    for p in parts:
        if not out:
            out = p[:limit]
        elif len(out) + 1 + len(p) <= limit:
            out += "-" + p
        else:
            break
    return out or "change"


def branch_name(kind: str, ticket_key: str, title: str) -> str:
    """GitHub flow: <type>/<PULSE-KEY>-<slug>, cut from and merged into the base branch."""
    if kind not in BRANCH_TYPES:
        raise ProjectError(f"branch type must be one of {BRANCH_TYPES}")
    return f"{kind}/{ticket_key}-{slugify(title)}"


def branch_problem(cfg: dict, branch: str, known_keys=None) -> Optional[str]:
    """Why a branch breaks the branching strategy, or None if it follows it."""
    base = cfg["git"]["base_branch"]
    if not safe_ref(branch):
        return f"'{branch}' is not a safe branch name (letters, digits, . _ - / only; no leading '-' or '..')"
    if branch == base or branch.lower() in PROTECTED:
        return f"'{branch}' is the base or a protected branch. Share a working branch."
    m = re.match(r"^(feature|bugfix|hotfix)/([A-Z][A-Z0-9]{1,9}-\d+)-([a-z0-9][a-z0-9-]*)$", branch)
    if not m:
        return (f"'{branch}' does not follow GitHub flow naming: <feature|bugfix|hotfix>/{cfg['key']}-<ticket number>-<short-description>, "
                f"for example feature/{cfg['key']}-123-returns-export")
    key = m.group(2)
    if not key.startswith(cfg["key"] + "-"):
        return f"branch references {key}, which is not a {cfg['key']} ticket"
    if known_keys is not None and key not in known_keys:
        return f"branch references {key}, which is not a ticket created for this flow ({', '.join(sorted(known_keys)) or 'none yet'})"
    return None


def with_defaults(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    out.setdefault("validation", {}).setdefault("require_tech_lead", False)
    out.setdefault("granola", {}).setdefault("source", "paste")
    out.setdefault("storage", {}).setdefault("backend", "jsonl")
    out["models"] = {**DEFAULT_MODELS, **(out.get("models") or {})}
    out.setdefault("escalate_after_failures", DEFAULT_ESCALATE_AFTER)
    out["runner"] = {**DEFAULT_RUNNER, **(out.get("runner") or {})}
    out.setdefault("git", {}).setdefault("strategy", "github-flow")
    out["schedules"] = {**DEFAULT_SCHEDULES, **(out.get("schedules") or {})}
    out.setdefault("mode", "warn")
    out.setdefault("tier_default", "T2")
    return out


def load_project(path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ProjectError(f"no project file at {p}. Create one with: dcp project init")
    try:
        return with_defaults(json.loads(p.read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        raise ProjectError(f"{p} is not valid JSON: {e}")


def project_path(projects_dir, key: str) -> Path:
    if not _KEY.match(key.upper()):
        raise ProjectError(f"'{key}' is not a valid project key")
    return Path(projects_dir) / f"{key.upper()}.json"


def store_path(data_dir, key: str, backend: str = "jsonl") -> Path:
    return Path(data_dir) / key.upper() / ("events.db" if backend == "sqlite" else "events.jsonl")
