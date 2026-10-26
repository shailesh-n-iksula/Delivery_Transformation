"""Further commands: people, score disputes, and importing hours and rework. Registered by cli.py."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

from .cli_ops import _ctx, _read, _safe
from .project import ProjectError, load_project, project_path, validate_project
from .scoring import CHECKS

ROLES = ("delivery_manager", "tech_lead", "delegate", "practice_lead", "senior")
_SLACK_ID = re.compile(r"^U[A-Z0-9]{6,}$")
HOW_TO_FIND_ID = ("Ask them for their Slack member ID (their profile, the three dots, Copy member ID; it looks like "
                  "U01ABC2345) and register them with: dcp people <KEY> add --role <role> --name <name> --slack-id <ID>")


def find_person(cfg: dict, slack_id: str = "", github: str = ""):
    """(role, person) for a Slack id or a GitHub login, or None."""
    for role, p in (cfg.get("people") or {}).items():
        if (slack_id and p.get("slack_id") == slack_id) or (github and p.get("github", "").lower() == github.lower()):
            return role, p
    return None


def require_person(cfg: dict, slack_id: str):
    hit = find_person(cfg, slack_id=slack_id)
    if not hit:
        raise ProjectError(f"Slack id {slack_id} is not a registered person on this project. {HOW_TO_FIND_ID}")
    return hit


# ---------- people ----------

@_safe
def cmd_people(args) -> int:
    path = project_path(args.projects_dir, args.project)
    cfg = load_project(path)
    people = cfg.setdefault("people", {})
    if args.action == "list":
        if not people:
            print("nobody registered yet")
        for role, p in people.items():
            extra = f"  github:{p['github']}" if p.get("github") else ""
            cov = f"  covers {', '.join(p['covers'])}" if p.get("covers") else ""
            print(f"{role:<17}{p['name']:<22}{p['slack_id']}{extra}{cov}")
        return 0
    if args.action == "whois":
        hit = find_person(cfg, args.slack_id or "", args.github or "")
        if not hit:
            print("not registered. " + HOW_TO_FIND_ID)
            return 1
        print(f"{hit[1]['name']} is the {hit[0]}")
        return 0
    # add
    if not (args.role and args.name and args.slack_id):
        raise ProjectError("add needs --role, --name and --slack-id")
    if args.role not in ROLES:
        raise ProjectError(f"role must be one of {', '.join(ROLES)}")
    if not _SLACK_ID.match(args.slack_id):
        raise ProjectError(f"'{args.slack_id}' is not a Slack member ID. {HOW_TO_FIND_ID}")
    for role, p in people.items():
        if p.get("slack_id") == args.slack_id and role != args.role and not (
                {role, args.role} <= {"senior", "practice_lead", "tech_lead", "delegate"}):
            raise ProjectError(f"{args.slack_id} is already registered as the {role}. A delivery manager cannot also hold another role.")
    if args.role in people and not args.replace:
        raise ProjectError(f"the {args.role} is already {people[args.role]['name']}. Use --replace to change it.")
    person = {"name": args.name, "slack_id": args.slack_id, "email": args.email or ""}
    if args.github:
        person["github"] = args.github
    if args.role == "delegate":
        person["covers"] = [c.strip() for c in (args.covers or "tech_lead").split(",") if c.strip()]
    new = {**cfg, "people": {**people, args.role: person}}
    problems = [p for p in validate_project(new) if p.startswith("people")]
    if problems:
        raise ProjectError("not saved: " + "; ".join(problems))
    path.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    print(f"registered {args.name} as {args.role}")
    return 0


# ---------- disputes ----------

@_safe
def cmd_dispute(args) -> int:
    cfg, store = _ctx(args)
    role, person = require_person(cfg, args.by_id)
    if args.check not in CHECKS:
        raise ProjectError(f"unknown check '{args.check}'. Known: {', '.join(sorted(CHECKS))}")
    if not any(e.flow == args.flow for e in store.events(flow=args.flow)):
        raise ProjectError(f"no such flow '{args.flow}'")
    if len(args.reason.strip()) < 10:
        raise ProjectError("give the reason for the dispute (a sentence); it is recorded")
    store.append("score.disputed", args.flow, cfg["key"], person["name"], {"check": args.check, "reason": args.reason.strip()},
                 track=cfg["track"])
    print(f"dispute recorded on {args.check} for {args.flow}. A practice lead resolves it.")
    return 0


@_safe
def cmd_dispute_resolve(args) -> int:
    cfg, store = _ctx(args)
    role, person = require_person(cfg, args.by_id)
    if role != "practice_lead":
        raise ProjectError(f"{person['name']} ({role}) cannot resolve a dispute. A practice lead does, so the people "
                           "being scored never judge their own score.")
    evs = store.events(flow=args.flow)
    open_ = [e for e in evs if e.type == "score.disputed" and e.data["check"] == args.check]
    if not open_:
        raise ProjectError(f"no dispute on {args.check} for {args.flow}")
    if len(args.note.strip()) < 10:
        raise ProjectError("give a note explaining the decision; it is recorded")
    store.append("score.dispute_resolved", args.flow, cfg["key"], person["name"],
                 {"check": args.check, "outcome": args.outcome, "note": args.note.strip()}, track=cfg["track"])
    print(f"{args.check} for {args.flow}: {args.outcome}")
    return 0


# ---------- break-glass ----------

@_safe
def cmd_breakglass(args) -> int:
    """A real emergency may skip steps. It is allowed, recorded with a reason, and must be reviewed afterwards."""
    cfg, store = _ctx(args)
    role, person = require_person(cfg, args.by_id)
    if len(args.reason.strip()) < 10:
        raise ProjectError("say what the emergency is (a sentence); it is recorded and reviewed afterwards")
    if not store.events(flow=args.flow):
        raise ProjectError(f"no such flow '{args.flow}'")
    store.append("breakglass.used", args.flow, cfg["key"], person["name"], {"reason": args.reason.strip()}, track=cfg["track"])
    print(f"recorded. Someone else must review this within 24 hours or it counts against {args.flow}.")
    return 0


@_safe
def cmd_breakglass_review(args) -> int:
    cfg, store = _ctx(args)
    role, person = require_person(cfg, args.by_id)
    used = store.events(flow=args.flow, type="breakglass.used")
    if not used:
        raise ProjectError(f"no break-glass use recorded on {args.flow}")
    if role not in ("practice_lead", "tech_lead", "delivery_manager"):
        raise ProjectError(f"{person['name']} ({role}) cannot review break-glass use")
    if used[-1].actor == person["name"]:
        raise ProjectError("the person who used break-glass cannot be the one who reviews it")
    store.append("breakglass.reviewed", args.flow, cfg["key"], person["name"], {"reviewer": person["name"]}, track=cfg["track"])
    print("review recorded")
    return 0


# ---------- imports ----------

def _ref(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _csv_rows(path: str):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


@_safe
def cmd_import_hours(args) -> int:
    cfg, store = _ctx(args)
    flows = set(store.flows(cfg["key"]))
    have = {e.data.get("import_ref") for e in store.events(project=cfg["key"], type="hours.recorded")}
    done = skipped = 0
    errors = []
    for n, row in enumerate(_csv_rows(args.csv), start=2):
        flow = (row.get("flow") or "").strip()
        try:
            est, act = float(row["estimate_hours"]), float(row["actual_hours"])
            if est <= 0 or act < 0:
                raise ValueError("estimate must be above 0 and actual cannot be negative")
            if flow not in flows:
                raise ValueError(f"no such flow '{flow}'")
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"line {n}: {e}")
            continue
        ref = _ref("hours", flow, est, act)
        if ref in have:
            skipped += 1
            continue
        store.append("hours.recorded", flow, cfg["key"], "import", {"estimate_hours": est, "actual_hours": act,
                     "import_ref": ref, "source": "csv"}, track=cfg["track"])
        have.add(ref)
        done += 1
    print(f"imported {done}, skipped {skipped} already imported" + (f", {len(errors)} problem(s):" if errors else ""))
    for e in errors:
        print("  " + e)
    return 1 if errors else 0


_REWORK = {"ticket.reopened": "ticket", "change_request.raised": "reason", "defect.found": "phase", "assumption_gap.found": "phase"}


@_safe
def cmd_import_rework(args) -> int:
    cfg, store = _ctx(args)
    flows = set(store.flows(cfg["key"]))
    have = {e.data.get("import_ref") for e in store.events(project=cfg["key"]) if e.data.get("import_ref")}
    done = skipped = 0
    errors = []
    for n, row in enumerate(_csv_rows(args.csv), start=2):
        flow, typ, detail = (row.get("flow") or "").strip(), (row.get("type") or "").strip(), (row.get("detail") or "").strip()
        try:
            if flow not in flows:
                raise ValueError(f"no such flow '{flow}'")
            if typ not in _REWORK:
                raise ValueError(f"type must be one of {', '.join(_REWORK)}")
            if not detail:
                raise ValueError("detail is required (ticket key, reason or phase)")
            if _REWORK[typ] == "phase" and detail not in ("dev", "uat", "prod"):
                raise ValueError("phase must be dev, uat or prod")
        except ValueError as e:
            errors.append(f"line {n}: {e}")
            continue
        ref = _ref("rework", flow, typ, detail, row.get("date", ""))
        if ref in have:
            skipped += 1
            continue
        store.append(typ, flow, cfg["key"], "import", {_REWORK[typ]: detail, "import_ref": ref, "source": "csv"}, track=cfg["track"])
        have.add(ref)
        done += 1
    print(f"imported {done}, skipped {skipped} already imported" + (f", {len(errors)} problem(s):" if errors else ""))
    for e in errors:
        print("  " + e)
    return 1 if errors else 0


@_safe
def cmd_worksheet(args) -> int:
    from .cli_ops import load_rules
    from .flowstate import next_orders
    from .project import store_path
    from .worksheet import build_worksheet, render
    cfg, store = _ctx(args)
    rules = load_rules(args.rules)
    orders, waiting = next_orders(store, cfg, None, rules)
    if args.flow:
        orders = [o for o in orders if o.flow == args.flow]
        waiting = [w for w in waiting if w.flow == args.flow]
    items = build_worksheet(cfg, [o.to_dict() for o in orders], [w.to_dict() for w in waiting], Path(args.data_dir),
                            str(store_path(args.data_dir, cfg["key"], cfg["storage"]["backend"])), rules)
    if args.json:
        print(json.dumps({"items": items, "waiting": [w.to_dict() for w in waiting]}, indent=2))
    else:
        print(render(items, [w.to_dict() for w in waiting]))
    return 0


@_safe
def cmd_serve(args) -> int:
    from .cli_ops import load_rules
    from .server import context_from_env, make_server
    ctx = context_from_env(args.projects_dir, args.data_dir, load_rules(args.rules))
    srv = make_server(ctx, args.host, args.port)
    print(f"listening on http://{args.host}:{srv.server_address[1]}  "
          f"(GitHub webhook {'on' if ctx.github_secret else 'OFF: set DCP_GITHUB_SECRET'}, "
          f"Slack webhook {'on' if ctx.slack_secret else 'OFF: set DCP_SLACK_SIGNING_SECRET'}, "
          f"read endpoints {'need a token' if ctx.api_token else 'open (localhost only)'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


@_safe
def cmd_webhook_test(args) -> int:
    from .webhooks import process_github
    cfg, store = _ctx(args)
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    res = process_github(args.event, payload, cfg, store, args.delivery or "")
    print(json.dumps(res, indent=2))
    return 0 if res.get("handled") else 1


@_safe
def cmd_repo_check(args) -> int:
    from . import repocheck
    res = repocheck.check_repo(Path(args.path), args.base, args.repo or "", args.github)
    if args.json:
        print(json.dumps({"summary": repocheck.summarise(res), "checks": res}, indent=2))
    else:
        print(repocheck.render(res))
    return 0 if repocheck.summarise(res)["ready"] else 1


@_safe
def cmd_pr_check(args) -> int:
    from . import prcheck
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    res = prcheck.check_pr(event, args.key.upper(), args.base)
    print(json.dumps(res, indent=2) if args.json else prcheck.render(res))
    return 0 if all(r["ok"] for r in res) else 1


@_safe
def cmd_dashboard(args) -> int:
    from .cli_ops import load_rules
    from .dashboard import render_dashboard
    from .events import open_store
    cfg, store = _ctx(args)
    if args.store:
        store = open_store(args.store)
    page = render_dashboard(cfg, store, load_rules(args.rules), label=args.label or "")
    Path(args.out).write_text(page, encoding="utf-8")
    print(f"wrote {args.out} ({len(page):,} bytes). Open it in a browser; it needs nothing else.")
    return 0


def register(sub) -> None:
    def common(sp):
        sp.add_argument("--project", required=True)
        sp.add_argument("--projects-dir", default="projects")
        sp.add_argument("--data-dir", default="data")

    sp = sub.add_parser("breakglass", help="record an emergency that skipped normal steps (reviewed within 24 hours)")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--by-id", required=True)
    sp.set_defaults(fn=cmd_breakglass)

    sp = sub.add_parser("breakglass-review", help="review a break-glass use (not the person who used it)")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--by-id", required=True)
    sp.set_defaults(fn=cmd_breakglass_review)

    sp = sub.add_parser("worksheet", help="manual mode: what is due, as a checklist with the exact messages and commands")
    common(sp)
    sp.add_argument("--flow")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_worksheet)

    sp = sub.add_parser("serve", help="run the reference server: webhooks in, orders and dashboard out")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)
    sp.add_argument("--projects-dir", default="projects")
    sp.add_argument("--data-dir", default="data")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("webhook-test", help="process a saved GitHub webhook payload locally (no signature), to see what it would log")
    common(sp)
    sp.add_argument("--event", required=True, choices=["pull_request", "pull_request_review", "issue_comment"])
    sp.add_argument("--file", required=True)
    sp.add_argument("--delivery")
    sp.set_defaults(fn=cmd_webhook_test)

    sp = sub.add_parser("repo-check", help="is a repo ready for this process? (files, and branch protection if GitHub is reachable)")
    sp.add_argument("--path", default=".", help="local clone of the repo")
    sp.add_argument("--base", default="main")
    sp.add_argument("--repo", help="owner/name, needed for --github")
    sp.add_argument("--github", action="store_true", help="also ask GitHub about branch protection (uses the gh command)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_repo_check)

    sp = sub.add_parser("pr-check", help="check a pull request against the template rules; for CI as a required status check")
    sp.add_argument("--event", required=True, help="path to the GitHub event JSON (in Actions: $GITHUB_EVENT_PATH)")
    sp.add_argument("--key", required=True, help="Pulse project key, e.g. SP")
    sp.add_argument("--base", default="main")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_pr_check)

    sp = sub.add_parser("dashboard", help="write the governance dashboard as one self-contained HTML file")
    common(sp)
    sp.add_argument("--out", default="dashboard.html")
    sp.add_argument("--store", help="read this log instead of the project's own (e.g. the synthetic sample)")
    sp.add_argument("--label", help="a banner shown at the top, e.g. SYNTHETIC DEMO DATA")
    sp.set_defaults(fn=cmd_dashboard)

    sp = sub.add_parser("people", help="list, add or look up the people on a project (by Slack member ID)")
    sp.add_argument("project")
    sp.add_argument("action", choices=["list", "add", "whois"])
    sp.add_argument("--role")
    sp.add_argument("--name")
    sp.add_argument("--slack-id")
    sp.add_argument("--email")
    sp.add_argument("--github", help="GitHub login, so pull request events can be matched to this person")
    sp.add_argument("--covers", help="delegate only: roles they cover, e.g. tech_lead")
    sp.add_argument("--replace", action="store_true")
    sp.add_argument("--projects-dir", default="projects")
    sp.set_defaults(fn=cmd_people)

    sp = sub.add_parser("dispute", help="dispute a compliance check on a flow")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--check", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--by-id", required=True)
    sp.set_defaults(fn=cmd_dispute)

    sp = sub.add_parser("dispute-resolve", help="a practice lead upholds or overturns a dispute")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--check", required=True)
    sp.add_argument("--outcome", required=True, choices=["upheld", "overturned"])
    sp.add_argument("--note", required=True)
    sp.add_argument("--by-id", required=True)
    sp.set_defaults(fn=cmd_dispute_resolve)

    sp = sub.add_parser("import-hours", help="import estimate vs actual hours per flow from a CSV (flow,estimate_hours,actual_hours)")
    common(sp)
    sp.add_argument("--csv", required=True)
    sp.set_defaults(fn=cmd_import_hours)

    sp = sub.add_parser("import-rework", help="import rework events from a CSV (flow,type,detail[,date])")
    common(sp)
    sp.add_argument("--csv", required=True)
    sp.set_defaults(fn=cmd_import_rework)
