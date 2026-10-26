"""Commands for the per-project orchestration layer. Registered onto the main CLI by cli.py."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

from .brief import BriefError, build_brief
from .config import load_rules
from .events import EventError, EventStore, copy_store, open_store, parse_ts
from .flowstate import can_approve, epoch_events, next_orders, required_roles, suggest_tier
from . import tokens
from .project import (BRANCH_TYPES, ProjectError, branch_name, branch_problem, key_from_board_url, load_project,
                      make_project, project_path, store_path, validate_project)
from .models import ACTION_KIND, STEP_LABEL, effective_models, tier_alias, validate_models
from .packlint import COVERAGE_LABELS, assess_call
from .pulseplan import (PlanError, apply_updates, build_plan, hashes_from_events, mapped_from_events, parse_sizes,
                        pulse_items_to_markdown, titles_from_events)
from .supportlint import build_support_ticket
from .ticketlint import lint_tickets

_HELP = ("Ask them for their Slack member ID (profile, three dots, Copy member ID; it looks like U01ABC2345) and register "
         "them with: dcp people <KEY> add --role <role> --name <name> --slack-id <ID>")
_TITLE = re.compile(r"^(.+) :: (.+) :: ([A-Z][A-Z0-9]{1,9})$")


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def _ctx(args):
    cfg = load_project(project_path(args.projects_dir, args.project))
    probs = validate_project(cfg)
    if probs and not getattr(args, "allow_incomplete", False):
        raise SystemExit("project config is incomplete:\n  " + "\n  ".join(probs)
                         + f"\nfix {project_path(args.projects_dir, args.project)} (dcp project validate {args.project})")
    return cfg, open_store(store_path(args.data_dir, cfg["key"], cfg["storage"]["backend"]))


def _safe(fn):
    def run(args):
        try:
            return fn(args)
        except (ProjectError, EventError, PlanError, BriefError) as e:
            problems = getattr(e, "problems", None)
            sys.exit("error: " + ("\n  " + "\n  ".join(problems) if problems else str(e)))
    return run


# ---------- project ----------

@_safe
def cmd_project_init(args) -> int:
    values = {
        "key": args.key, "track": args.track, "board_url": args.pulse_url, "slack_channel": args.slack_channel,
        "granola_folder": args.granola_folder, "git_repo": args.git_repo, "delivery_manager": args.delivery_manager,
        "tech_lead": args.tech_lead,
    }
    if args.pulse_url and not args.key:
        values["key"] = key_from_board_url(args.pulse_url) or ""
    asks = [("track", "Track (build or support)"), ("board_url", "Pulse board URL"),
            ("slack_channel", "Slack channel for gates and nudges (#name)"),
            ("tech_lead", "Tech lead (Name:SlackID)")]
    if (values["track"] or "build") == "build":
        asks += ([("granola_folder", "Granola folder for this project's calls")] if args.granola_source == "connector" else [])
        asks += [("git_repo", "GitHub repo (owner/name)"),
                 ("delivery_manager", "Delivery manager (Name:SlackID)")]
    if sys.stdin.isatty():
        for k, q in asks:
            if not values[k]:
                values[k] = input(f"{q}: ").strip()
        if not values["key"]:
            values["key"] = (key_from_board_url(values["board_url"] or "") or input("Project key: ")).strip()
    cfg = make_project(
        key=values["key"] or "", track=values["track"] or "build", board_url=values["board_url"] or "",
        slack_channel=values["slack_channel"] or "", granola_folder=values["granola_folder"] or "",
        granola_source=args.granola_source,
        git_repo=values["git_repo"] or "", base_branch=args.base_branch, delivery_manager=values["delivery_manager"] or "",
        tech_lead=values["tech_lead"] or "", delegate=args.delegate or "", name=args.name or "",
        support_parent=args.support_parent or "", intake_channel=args.intake_channel or "", mode=args.mode)
    path = project_path(args.projects_dir, cfg["key"])
    if path.exists() and not args.force:
        raise ProjectError(f"{path} already exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    probs = validate_project(cfg)
    print("ready to run" if not probs else "still to fill in:\n  " + "\n  ".join(probs))
    return 0 if not probs else 1


@_safe
def cmd_project_validate(args) -> int:
    cfg = load_project(project_path(args.projects_dir, args.project))
    probs = validate_project(cfg)
    if probs:
        print(f"{cfg.get('key', args.project)}: not ready\n  " + "\n  ".join(probs))
        return 1
    print(f"{cfg['key']}: ready ({cfg['track']} track, mode {cfg['mode']})")
    return 0


@_safe
def cmd_project_show(args) -> int:
    print(json.dumps(load_project(project_path(args.projects_dir, args.project)), indent=2))
    return 0


# ---------- scheduler ----------

@_safe
def cmd_next(args) -> int:
    cfg, store = _ctx(args)
    orders, waiting = next_orders(store, cfg, parse_ts(args.now) if args.now else None, load_rules(args.rules))
    if args.json:
        print(json.dumps({"project": cfg["key"], "mode": cfg["mode"], "needs_ai": any(o.needs_ai for o in orders),
                          "orders": [o.to_dict() for o in orders],
                          "waiting": [w.to_dict() for w in waiting]}, indent=2))
        return 0
    print(f"{cfg['key']} ({cfg['track']}, mode {cfg['mode']})")
    print("\nDO NOW" if orders else "\nnothing due (no tokens needed)")
    for o in orders:
        who = f"{o.tier}:{o.model}" if o.model else "no model"
        print(f"  [{o.kind}|{who}] {o.flow} -> {o.action}: {o.why}")
    if waiting:
        print("\nWAITING ON PEOPLE")
        for w in waiting:
            since = w.since.strftime("%Y-%m-%d %H:%M") if w.since else "-"
            print(f"  {w.flow}: {w.waiting_for} ({w.owner}{', optional' if w.optional else ''}) since {since}. {w.detail}")
    return 0


# ---------- intake ----------

def register_flow(cfg: dict, store, ref: str, title=None, text=None, actor: str = "dcp", meta=None):
    """Registers a call note or support request as a flow. Returns (exit code, result dict). Deduped by `ref`."""
    key = cfg["key"]
    kind = "support" if cfg["track"] == "support" else "build"
    for e in store.events(project=key):
        if e.type in ("call.recorded", "intake.received") and e.data.get("source_ref") == ref:
            return 0, {"flow": e.flow, "status": "exists"}
    status = "ok"
    if kind == "build":
        m = _TITLE.match(title or "")
        if m and m.group(3) != key:
            return 2, {"flow": None, "status": "wrong_project",
                       "detail": f"note is tagged '{m.group(3)}' but this folder belongs to {key}"}
        status = "ok" if m else "needs_rename"
    tag = "REQ" if kind == "build" else "SUP"
    nums = [int(m.group(1)) for f in store.flows(key) if (m := re.match(rf"^{key}-{tag}-(\d+)$", f))]
    flow = f"{key}-{tag}-{(max(nums) + 1 if nums else 1):03d}"
    if kind == "build":
        data = {"title": title, "source_ref": ref}
        data.update({k: v for k, v in (meta or {}).items() if v})
        store.append("call.recorded", flow, key, actor, data, track="build")
    else:
        store.append("intake.received", flow, key, actor,
                     {"source_ref": ref, "text": text or title or "", "suggested_tier": suggest_tier(text or title or "")},
                     track="support")
    out = {"flow": flow, "status": status}
    if status == "needs_rename":
        out["detail"] = "Title should read 'Project :: topic :: KEY'. Ask the delivery manager to rename it."
    return 0, out


@_safe
def cmd_flow_new(args) -> int:
    cfg, store = _ctx(args)
    code, out = register_flow(cfg, store, args.source_ref, args.title, args.text, args.actor)
    print(json.dumps(out))
    return code


@_safe
def cmd_ingest(args) -> int:
    from .ingest import ingest_inbox
    cfg, store = _ctx(args)
    res = ingest_inbox(cfg, Path(args.data_dir), lambda c, ref, title, meta: register_flow(c, store, ref, title, None, "dcp", meta), store)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    print(f"inbox: {res['inbox']}")
    for i in res["ingested"]:
        print(f"  ingested {i['file']} -> {i['flow']} ({i['words']} words, title '{i['title']}')")
    for a in res["attached"]:
        print(f"  follow-up {a['file']} added to {a['flow']} ({a['words']} words)" + ("; the pack is reopened for revision" if a["reopened"] else "; the call will be assessed again"))
    for d in res["duplicates"]:
        print(f"  duplicate {d['file']} (already {d['flow']}), filed away")
    for r in res["rejected"]:
        print(f"  REFUSED {r['file']}: {r['reason']}")
    for n in res["needs_rename"]:
        print(f"  {n['flow']}: title '{n['title']}' should read 'Project :: topic :: KEY'")
    if not any(res[k] for k in ("ingested", "duplicates", "rejected", "attached")):
        print("  nothing waiting")
    return 1 if res["rejected"] else 0


@_safe
def cmd_retitle(args) -> int:
    cfg, store = _ctx(args)
    store.append("call.retitled", args.flow, cfg["key"], args.actor, {"title": args.title})
    print("retitled" if _TITLE.match(args.title) else "retitled, but still not 'Project :: topic :: KEY'")
    return 0


# ---------- gates, tier, branch ----------

def _resolve_approver(cfg: dict, rules: dict, slack_id: str, gate: int):
    allowed = rules["compliance"]["roles"].get(f"gate{gate}_approvers", [])
    for role, person in (cfg.get("people") or {}).items():
        if person.get("slack_id") == slack_id:
            if role in allowed:
                return person["name"], role, False
            covers = person.get("covers") or []
            if role == "delegate":
                for c in covers:
                    if c in allowed:
                        return person["name"], c, True
            raise ProjectError(f"{person['name']} ({role}) cannot approve Gate {gate}. Allowed: {allowed}")
    raise ProjectError(f"Slack id {slack_id} is not a registered person on this project, so cannot approve. {_HELP}")


@_safe
def cmd_gate_open(args) -> int:
    cfg, store = _ctx(args)
    data = {"gate": args.gate}
    if args.gate == 1:
        packs = [e for e in store.events(flow=args.flow) if e.type == "pack.drafted"]
        if not packs:
            raise ProjectError("no pack to show the reviewer")
        data["pack_version"] = max(p.data["version"] for p in packs)
    if args.gate in (1, 2):
        data["required_roles"] = ["delivery_manager"] + (
            ["tech_lead"] if args.gate == 2 and cfg["validation"]["require_tech_lead"] else [])
    store.append("gate.opened", args.flow, cfg["key"], args.actor, data, track=cfg["track"])
    print(f"gate {args.gate} opened")
    return 0


@_safe
def cmd_gate_approve(args) -> int:
    cfg, store = _ctx(args)
    rules = load_rules(args.rules)
    name, role, delegated = _resolve_approver(cfg, rules, args.approver_id, args.gate)
    evs = store.events(flow=args.flow)
    reasons = can_approve(evs, args.gate)
    if reasons:
        print(f"Gate {args.gate} cannot be approved yet:\n  - " + "\n  - ".join(reasons))
        return 1
    data = {"gate": args.gate, "approver": name, "approver_role": role}
    if args.gate == 1:
        packs = [e for e in evs if e.type == "pack.drafted"]
        data["pack_version"] = max(p.data["version"] for p in packs)
    if delegated:
        data["delegated"] = True
    store.append("gate.approved", args.flow, cfg["key"], name, data, track=cfg["track"])
    after = store.events(flow=args.flow)
    done = {e.data["approver_role"] for e in after if e.type == "gate.approved" and str(e.data["gate"]) == str(args.gate)}
    left = [r for r in required_roles(after, args.gate) if r not in done]
    print(f"gate {args.gate} approved by {name} ({role}{', as delegate' if delegated else ''})"
          + (f"; still needs {', '.join(left)}" if left else "; gate complete"))
    return 0


@_safe
def cmd_tier_set(args) -> int:
    cfg, store = _ctx(args)
    rules = load_rules(args.rules)
    roles = rules["compliance"]["roles"]["tier_setters"]
    name, role, _ = (None, None, None)
    for r, person in (cfg.get("people") or {}).items():
        if person.get("slack_id") == args.setter_id:
            name, role = person["name"], r
    if not name:
        raise ProjectError(f"Slack id {args.setter_id} is not a registered person on this project. {_HELP}")
    if role not in roles:
        raise ProjectError(f"{name} ({role}) cannot set the tier. Leads set it. Allowed: {roles}")
    if args.tier.upper() not in ("T1", "T2", "T3"):
        raise ProjectError("tier must be T1, T2 or T3")
    store.append("tier.set", args.flow, cfg["key"], name, {"tier": args.tier.upper(), "actor_role": role}, track=cfg["track"])
    print(f"tier {args.tier.upper()} set by {name}")
    return 0


def known_keys(evs) -> set:
    """Pulse keys a branch may reference: the flow's stories, or the support ticket. Not epics, risks or dependencies."""
    keys = set()
    for e in evs:
        if e.type == "pulse.mapped" and re.match(r"^s\d+$", e.data["local_id"]):
            keys.add(e.data["key"])
        elif e.type == "ticket.created":
            keys.add(e.data["ticket"])
    return keys


@_safe
def cmd_branch_name(args) -> int:
    cfg, store = _ctx(args)
    keys = known_keys(store.events(flow=args.flow))
    if args.key not in keys:
        raise ProjectError(f"{args.key} is not a story or ticket created for {args.flow} ({', '.join(sorted(keys)) or 'none yet'})")
    print(branch_name(args.type, args.key, args.title))
    return 0


@_safe
def cmd_branch_share(args) -> int:
    cfg, store = _ctx(args)
    repo = args.repo or cfg["git"].get("repo", "")
    if not repo:
        raise ProjectError("no repo: set git.repo in the project file or pass --repo")
    problem = branch_problem(cfg, args.branch, known_keys(store.events(flow=args.flow)))
    if problem:
        raise ProjectError(problem)
    store.append("branch.shared", args.flow, cfg["key"], args.actor, {"repo": repo, "branch": args.branch}, track=cfg["track"])
    cmd = (f"cd <local clone of {repo}> && git fetch origin && git checkout {shlex.quote(args.branch)} "
           f"&& claude -p \"$(cat briefs/{args.flow}.md)\"")
    print(f"branch {args.branch} recorded for {args.flow}\nlaunch (tech lead runs this): {cmd}")
    return 0


@_safe
def cmd_no_code(args) -> int:
    cfg, store = _ctx(args)
    store.append("code.not_needed", args.flow, cfg["key"], args.actor, {"reason": args.reason}, track=cfg["track"])
    print("recorded: no code change needed")
    return 0


# ---------- change requests ----------

@_safe
def cmd_cr_open(args) -> int:
    cfg, store = _ctx(args)
    role_person = next(((r, p) for r, p in (cfg.get("people") or {}).items() if p.get("slack_id") == args.by_id), None)
    if not role_person:
        raise ProjectError(f"Slack id {args.by_id} is not a registered person on this project. {_HELP}")
    role, person = role_person
    if role != "delivery_manager":
        raise ProjectError(f"{person['name']} ({role}) cannot open a change request. The delivery manager does, with the client's agreement.")
    evs = store.events(flow=args.flow)
    packs = [e for e in evs if e.type == "pack.drafted"]
    if not packs:
        raise ProjectError("there is no pack to change yet. Add a follow-up call instead, or edit the transcript before drafting.")
    if len(args.reason.strip()) < 10:
        raise ProjectError("give the change in a sentence; it is recorded and shown to the reviewer")
    last_reopen = max((i for i, e in enumerate(evs) if e.type == "pack.reopened"), default=-1)
    last_draft = max(i for i, e in enumerate(evs) if e.type == "pack.drafted")
    if last_reopen > last_draft:
        raise ProjectError("a change is already open and waiting for the revised pack")
    store.append("change_request.raised", args.flow, cfg["key"], person["name"], {"reason": args.reason.strip()}, track=cfg["track"])
    store.append("pack.reopened", args.flow, cfg["key"], person["name"],
                 {"reason": args.reason.strip(), "from_version": max(p.data["version"] for p in packs)}, track=cfg["track"])
    print("change request recorded; the pack, Gate 1, the tickets and Gate 2 will be redone against the new version")
    return 0


# ---------- Pulse tickets ----------

def _latest_pack_ref(store, flow):
    packs = [e for e in store.events(flow=flow) if e.type == "pack.drafted"]
    v = max((p.data["version"] for p in packs), default=1)
    return f"Requirement Pack {flow} v{v}"


@_safe
def cmd_tickets_plan(args) -> int:
    cfg, store = _ctx(args)
    evs = store.events(flow=args.flow)
    mapped = mapped_from_events(evs)
    tier = next((e.data["tier"] for e in reversed(evs) if e.type == "tier.set"), None)
    sizes = {}
    for e in evs:
        if e.type == "tech.sized":
            sizes.update(e.data["sizes"])
    versions = [e.data["version"] for e in evs if e.type == "pack.drafted"]
    plan = build_plan(_read(args.tickets), args.flow, _latest_pack_ref(store, args.flow),
                      cfg["pulse"]["project_key"], mapped, tier, sizes, hashes_from_events(evs), titles_from_events(evs),
                      max(versions) if versions else None)
    if plan["creates_total"] == plan["creates_done"] and not any(e.type == "tickets.created" for e in epoch_events(evs)):
        store.append("tickets.created", args.flow, cfg["key"], "dcp", {"count": plan["creates_done"],
                     "epic_key": mapped.get("epic", "")}, track=cfg["track"])
    print(json.dumps(plan, indent=2))
    return 0


@_safe
def cmd_tickets_record(args) -> int:
    cfg, store = _ctx(args)
    data = {"local_id": args.local_id, "key": args.key}
    if args.hash:
        data["hash"] = args.hash
    if args.title:
        data["title"] = args.title
    store.append("pulse.mapped", args.flow, cfg["key"], args.actor, data, track=cfg["track"])
    print(f"recorded {args.local_id} -> {args.key}")
    return 0


@_safe
def cmd_tickets_size(args) -> int:
    cfg, store = _ctx(args)
    sizes = parse_sizes((args.sizes or "").replace(",", " "))
    closed = [c.strip() for c in (args.close or "").split(",") if c.strip()]
    if args.sizes and not sizes:
        raise ProjectError("no sizes understood. Use 'S1=5d S2=3d' (d = days, h = hours)")
    if not sizes and not closed:
        raise ProjectError("nothing to record: give --sizes and/or --close")
    name, role = None, None
    for r, person in (cfg.get("people") or {}).items():
        if person.get("slack_id") == args.by_id:
            name, role = person["name"], r
    if not name:
        raise ProjectError(f"Slack id {args.by_id} is not a registered person on this project. {_HELP}")
    evs = store.events(flow=args.flow)
    if sizes:
        # Sizing is its own optional step: the tech lead (or the delegate covering them), any time after the
        # tickets exist and before development starts. It is not part of validation.
        covers = (cfg["people"].get("delegate") or {}).get("covers", []) if role == "delegate" else []
        if role != "tech_lead" and "tech_lead" not in covers:
            raise ProjectError(f"{name} ({role}) cannot size. Sizes come from the tech lead.")
        if not any(e.type == "tickets.created" for e in evs):
            raise ProjectError("the tickets have not been created in Pulse yet, so there is nothing to size")
        if any(e.type == "build.started" for e in evs):
            raise ProjectError("development has started; sizing is closed")
    elif role not in ("delivery_manager", "tech_lead"):
        raise ProjectError(f"{name} ({role}) cannot close decisions")
    text = apply_updates(_read(args.tickets), sizes, closed)
    Path(args.out or args.tickets).write_text(text, encoding="utf-8")
    if sizes:
        store.append("tech.sized", args.flow, cfg["key"], name, {"sizes": sizes, "by_role": role}, track=cfg["track"])
    if closed:
        store.append("decision.closed", args.flow, cfg["key"], name, {"titles": closed}, track=cfg["track"])
    print((f"sizes recorded: {sizes}" if sizes else "") + (f"; decisions closed: {closed}" if closed else ""))
    return 0


@_safe
def cmd_support_ticket(args) -> int:
    cfg, store = _ctx(args)
    rules = load_rules(args.rules)
    fields = json.loads(_read(args.fields))
    out = build_support_ticket(fields, args.title, cfg["pulse"].get("title_prefix", ""), rules, args.flow)
    out["create"]["projectKey"] = cfg["pulse"]["project_key"]
    out["parent_note"] = (f"After creating, comment on the new ticket: 'Parent: {cfg['pulse']['support_parent']}' "
                          "(the connector cannot set a parent).") if cfg["pulse"].get("support_parent") else ""
    print(json.dumps(out, indent=2))
    return 0 if out["lint"]["passed"] else 1


@_safe
def cmd_lint_pulse(args) -> int:
    cfg, _ = _ctx(args)
    rules = load_rules(args.rules)
    items = json.loads(_read(args.export))
    md = pulse_items_to_markdown(items)
    res = lint_tickets(md, rules, args.stage, name=f"{cfg['key']} board sweep")
    print(json.dumps(res.to_dict(), indent=2) if args.json else res.render())
    return 0 if res.passed else 1


# ---------- storage ----------

@_safe
def cmd_migrate_store(args) -> int:
    path = project_path(args.projects_dir, args.project)
    cfg = load_project(path)
    src_backend = cfg["storage"]["backend"]
    if src_backend == args.to:
        raise ProjectError(f"{cfg['key']} already uses {args.to}")
    src = open_store(store_path(args.data_dir, cfg["key"], src_backend))
    dst = open_store(store_path(args.data_dir, cfg["key"], args.to))
    problems = src.verify()
    if problems:
        raise ProjectError("the current log fails its integrity check, so it was not migrated:\n  " + "\n  ".join(problems))
    n = copy_store(src, dst)
    problems = dst.verify()
    if problems or len(dst.events()) != len(src.events()):
        raise ProjectError("the copy does not match the original; the project file was not changed")
    cfg["storage"]["backend"] = args.to
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"moved {n} events to {args.to}; chain verified. The old log is left in place as a backup.")
    return 0


# ---------- is the call ready to draft from ----------

@_safe
def cmd_assess_call(args) -> int:
    cfg, store = _ctx(args)
    rules = load_rules(args.rules)
    path = Path(args.transcript) if args.transcript else Path(args.data_dir) / cfg["key"] / args.flow / "transcript.txt"
    if not path.exists():
        raise ProjectError(f"no transcript at {path}")
    from .ingest import source_problem
    text = path.read_text(encoding="utf-8")
    res = assess_call(text, rules)
    problem = source_problem(text)
    if problem:
        res.update({"covered": [], "missing": list(COVERAGE_LABELS), "ready": False})
    data = {k: res[k] for k in ("covered", "missing", "ready", "needed", "words")}
    if problem:
        data["problem"] = problem
    store.append("call.assessed", args.flow, cfg["key"], "dcp", data, track=cfg["track"])
    if problem:
        print(f"{args.flow}: NOT ready. The source is not usable: {problem}")
        return 0
    print(f"{args.flow}: covers {len(res['covered'])} of 6 must-haves ({res['words']} words); needs {res['needed']}. "
          + ("READY to draft." if res["ready"] else "NOT ready: this is a follow-up call, not a requirement."))
    for k in res["missing"]:
        print(f"  not found: {COVERAGE_LABELS.get(k, k)}")
    return 0


@_safe
def cmd_call_proceed(args) -> int:
    cfg, store = _ctx(args)
    person = next(((p["name"], r) for r, p in (cfg.get("people") or {}).items() if p.get("slack_id") == args.by_id), None)
    if not person:
        raise ProjectError(f"Slack id {args.by_id} is not a registered person on this project. {_HELP}")
    if person[1] != "delivery_manager":
        raise ProjectError(f"{person[0]} ({person[1]}) cannot override. The delivery manager decides whether to draft from a thin call.")
    if len(args.reason.strip()) < 8:
        raise ProjectError("give a real reason (at least a short sentence); it is recorded")
    store.append("call.override", args.flow, cfg["key"], person[0], {"reason": args.reason.strip()}, track=cfg["track"])
    print("recorded: drafting will proceed from this call")
    return 0


# ---------- models, tokens, tick ----------

@_safe
def cmd_models(args) -> int:
    path = project_path(args.projects_dir, args.project)
    cfg = load_project(path)
    changed = False
    for item in args.set or []:
        if "=" not in item:
            raise ProjectError(f"'{item}' must look like step=tier, e.g. draft_pack=strong")
        step, tier = (x.strip() for x in item.split("=", 1))
        cfg["models"][step] = tier
        changed = True
    if args.escalate_after:
        cfg["escalate_after_failures"] = args.escalate_after
        changed = True
    if args.runner_mode:
        cfg["runner"]["mode"] = args.runner_mode
        changed = True
    problems = validate_models(cfg)
    if problems:
        print("not saved:\n  " + "\n  ".join(problems))
        return 1
    if changed:
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"saved {path}")
    rules = load_rules(args.rules)
    print(f"runner mode: {cfg['runner']['mode']}   escalate one tier after {cfg['escalate_after_failures']} failed checks\n")
    print(f"{'step':<24}{'kind':<11}{'tier':<10}{'model':<8}what it does")
    models = effective_models(cfg)
    for step in sorted(ACTION_KIND, key=lambda s: (["ai", "connector", "code"].index(ACTION_KIND[s]), s)):
        t = models[step]
        print(f"{step:<24}{ACTION_KIND[step]:<11}{t:<10}{(tier_alias(t, rules) or '-'):<8}{STEP_LABEL[step]}")
    return 0


@_safe
def cmd_ai_used(args) -> int:
    cfg, store = _ctx(args)
    for name in ("input_tokens", "output_tokens"):
        if getattr(args, name) < 0:
            raise ProjectError(f"{name} cannot be negative")
    if ACTION_KIND.get(args.action) == "code":
        raise ProjectError(f"'{args.action}' is plain code and uses no model. Do not log tokens for it.")
    if args.action not in ACTION_KIND:
        raise ProjectError(f"unknown step '{args.action}'")
    data = {"action": args.action, "model": args.model, "input_tokens": args.input_tokens, "output_tokens": args.output_tokens}
    if args.tier:
        data["tier"] = args.tier
    store.append("ai.used", args.flow, cfg["key"], args.actor, data, track=cfg["track"])
    print(f"logged {args.input_tokens + args.output_tokens} tokens for {args.action} on {args.model}")
    return 0


@_safe
def cmd_tokens(args) -> int:
    cfg, store = _ctx(args)
    rules = load_rules(args.rules)
    evs = store.events(project=cfg["key"])
    s = tokens.summarize(evs, rules)
    out = {"summary": s, "per_flow": tokens.per_flow(evs, rules), "draft_outcomes_by_model": tokens.outcomes(evs)}
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    t = s["total"]
    if not t["calls"]:
        print("no AI calls logged yet (the runner logs one `dcp ai-used` per model call)")
        return 0
    print(f"{cfg['key']}: {t['calls']} AI calls, {t['tokens']:,} tokens ({t['input']:,} in, {t['output']:,} out), "
          f"{t['cost_units']} relative cost units (placeholder weights)")
    print(f"\n{'step':<24}{'calls':>6}{'avg tokens':>12}{'tokens':>10}{'cost units':>12}")
    for a, b in sorted(s["by_action"].items(), key=lambda kv: -(kv[1]['input'] + kv[1]['output'])):
        print(f"{a:<24}{b['calls']:>6}{b['avg_tokens_per_call']:>12,}{b['input'] + b['output']:>10,}{b['cost_units']:>12}")
    print(f"\n{'model':<24}{'calls':>6}{'tokens':>10}{'cost units':>12}")
    for m, b in s["by_model"].items():
        print(f"{m:<24}{b['calls']:>6}{b['input'] + b['output']:>10,}{b['cost_units']:>12}")
    oc = out["draft_outcomes_by_model"]
    if oc:
        print(f"\n{'model':<24}{'drafts':>7}{'accepted':>10}{'first try':>11}")
        for m, b in oc.items():
            ft = f"{b['first_try_pass_rate']}%" if b["first_try_pass_rate"] is not None else "-"
            print(f"{m:<24}{b['drafts']:>7}{b['passed']:>10}{ft:>11}")
    print("\nper flow:")
    for f, b in out["per_flow"].items():
        print(f"  {f}: {b['tokens']:,} tokens, {b['calls']} calls")
    if t["unpriced_tokens"]:
        print(f"\nnote: {t['unpriced_tokens']:,} tokens are on models with no price weight in rules.json")
    return 0


@_safe
def cmd_tick(args) -> int:
    from .tick import launch as launch_sessions
    from .tick import tick as run_tick
    cfg, _ = _ctx(args)
    rules = load_rules(args.rules)
    res = run_tick(cfg, rules, Path(args.data_dir), store_path(args.data_dir, cfg["key"], cfg["storage"]["backend"]),
                   Path(args.projects_dir))
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"{cfg['key']}: ran {len(res['ran_without_ai'])} code step(s) with no tokens" + (f" ({', '.join(res['ran_without_ai'])})" if res["ran_without_ai"] else ""))
        ing = res["ingest"]
        for i in ing["ingested"]:
            print(f"  ingested {i['file']} -> {i['flow']} ({i['words']} words)")
        for r in ing["rejected"]:
            print(f"  REFUSED {r['file']}: {r['reason']}")
        for n in res["skipped"]:
            print(f"  skipped: {n}")
        if res["idle"]:
            print("idle: nothing needs a model this tick. No AI session started.")
        for s in res["sessions"]:
            print(f"needs a session on '{s['model']}': " + ", ".join(f"{o['flow']}:{o['action']}" for o in s["orders"]))
    if args.launch and res["sessions"]:
        for note in launch_sessions(res, Path("runner/RUNNER.md")):
            print("launched " + note)
    return 0


# ---------- build brief ----------

@_safe
def cmd_build_brief(args) -> int:
    cfg, store = _ctx(args)
    rules = load_rules(args.rules)
    evs = store.events(flow=args.flow)
    branch = args.branch or next((e.data["branch"] for e in reversed(evs) if e.type == "branch.shared"), "")
    tier = args.tier or next((e.data["tier"] for e in reversed(evs) if e.type == "tier.set"), "")
    if not branch:
        raise ProjectError("no branch shared for this flow yet (dcp branch-share)")
    gates_ok = cfg["track"] != "build" or all(
        any(e.type == "gate.approved" and str(e.data["gate"]) == str(g) for e in evs) for g in (1, 2))
    if not gates_ok:
        raise ProjectError("Gate 1 and Gate 2 must both be approved before a build brief is released")
    text, warnings = build_brief(_read(args.pack), _read(args.tickets), cfg, rules, args.flow, branch, tier,
                                 _read(args.rules_text) if args.rules_text else None,
                                 mapped_from_events(evs), known_keys=known_keys(evs))
    out = Path(args.out or f"briefs/{args.flow}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    store.append("brief.built", args.flow, cfg["key"], "dcp", {"path": str(out)}, track=cfg["track"])
    print(f"brief written to {out}")
    for w in warnings:
        print(f"warning: {w}")
    return 0


def register(sub) -> None:
    def common(sp, project=True):
        if project:
            sp.add_argument("--project", required=True)
        sp.add_argument("--projects-dir", default="projects")
        sp.add_argument("--data-dir", default="data")
        sp.add_argument("--allow-incomplete", action="store_true", help=argparse.SUPPRESS)

    p = sub.add_parser("project", help="register or inspect a project")
    psub = p.add_subparsers(dest="pcmd", required=True)
    sp = psub.add_parser("init", help="register a project with the few inputs it needs")
    sp.add_argument("--key")
    sp.add_argument("--track", choices=["build", "support"])
    sp.add_argument("--pulse-url", help="Pulse board URL; the key is read from it")
    sp.add_argument("--slack-channel")
    sp.add_argument("--intake-channel", help="support: channel where requests arrive (defaults to the main channel)")
    sp.add_argument("--granola-folder")
    sp.add_argument("--granola-source", choices=["paste", "connector"], default="paste",
                    help="paste: transcripts are dropped in data/<KEY>/inbox/ (default); connector: read from Granola")
    sp.add_argument("--git-repo", help="owner/name")
    sp.add_argument("--base-branch", default="main")
    sp.add_argument("--delivery-manager", help="Name:SlackID[:email]")
    sp.add_argument("--tech-lead", help="Name:SlackID[:email]")
    sp.add_argument("--delegate", help="Name:SlackID[:email]")
    sp.add_argument("--support-parent", help="support: Pulse key of the monthly support story")
    sp.add_argument("--name")
    sp.add_argument("--mode", choices=["warn", "enforce"], default="warn")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--projects-dir", default="projects")
    sp.set_defaults(fn=cmd_project_init)
    for name, fn in (("validate", cmd_project_validate), ("show", cmd_project_show)):
        sp = psub.add_parser(name)
        sp.add_argument("project")
        sp.add_argument("--projects-dir", default="projects")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("next", help="what is due now for a project, and who is being waited on")
    common(sp)
    sp.add_argument("--now")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_next)

    sp = sub.add_parser("flow-new", help="register a new call note or support request as a flow")
    common(sp)
    sp.add_argument("--source-ref", required=True, help="Granola note id or Slack message link (dedupes)")
    sp.add_argument("--title", help="Granola note title")
    sp.add_argument("--text", help="support: the request text")
    sp.add_argument("--actor", default="dcp")
    sp.set_defaults(fn=cmd_flow_new)

    sp = sub.add_parser("ingest", help="paste mode: pick up transcripts dropped in data/<KEY>/inbox/")
    common(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_ingest)

    sp = sub.add_parser("retitle", help="record that the Granola note was renamed")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--actor", default="dcp")
    sp.set_defaults(fn=cmd_retitle)

    sp = sub.add_parser("gate-open")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--gate", type=int, required=True, choices=[1, 2, 3])
    sp.add_argument("--actor", default="dcp")
    sp.set_defaults(fn=cmd_gate_open)

    sp = sub.add_parser("gate-approve", help="approve a gate; refused unless its preconditions hold")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--gate", type=int, required=True, choices=[1, 2, 3])
    sp.add_argument("--approver-id", required=True, help="Slack member id of the person who approved")
    sp.set_defaults(fn=cmd_gate_approve)

    sp = sub.add_parser("tier-set")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--tier", required=True)
    sp.add_argument("--setter-id", required=True, help="Slack member id of the lead")
    sp.set_defaults(fn=cmd_tier_set)

    sp = sub.add_parser("branch-share", help="the tech lead shares the git branch the build starts from")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--branch", required=True)
    sp.add_argument("--repo")
    sp.add_argument("--actor", required=True)
    sp.set_defaults(fn=cmd_branch_share)

    sp = sub.add_parser("branch-name", help="the GitHub-flow branch name for a ticket")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--key", required=True, help="Pulse key of the story or ticket")
    sp.add_argument("--title", required=True)
    sp.add_argument("--type", default="feature", choices=list(BRANCH_TYPES))
    sp.set_defaults(fn=cmd_branch_name)

    sp = sub.add_parser("no-code")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--actor", required=True)
    sp.set_defaults(fn=cmd_no_code)

    sp = sub.add_parser("cr-open", help="the client changed the requirement after the pack was drafted")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--by-id", required=True)
    sp.set_defaults(fn=cmd_cr_open)

    sp = sub.add_parser("tickets-plan", help="Pulse operations still to do for a ticket set (resumable)")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--tickets", required=True)
    sp.set_defaults(fn=cmd_tickets_plan)

    sp = sub.add_parser("tickets-record", help="record that one planned operation was done in Pulse")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--local-id", required=True)
    sp.add_argument("--key", required=True, help="Pulse issue key (for a comment: the key it was added to)")
    sp.add_argument("--hash", help="the op's hash, so a later change to the ticket is noticed")
    sp.add_argument("--title", help="the op's title, so a reordered ticket set is noticed")
    sp.add_argument("--actor", default="dcp")
    sp.set_defaults(fn=cmd_tickets_record)

    sp = sub.add_parser("tickets-size", help="merge the tech lead's sizes / closed decisions into the ticket set")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--tickets", required=True)
    sp.add_argument("--sizes", help="e.g. 'S1=5d S2=3d' (tech lead; optional unless the project requires the tech lead)")
    sp.add_argument("--close", help="comma-separated decision titles that are now decided")
    sp.add_argument("--out")
    sp.add_argument("--by-id", required=True, help="Slack member id of who is recording this (tech lead for sizes)")
    sp.set_defaults(fn=cmd_tickets_size)

    sp = sub.add_parser("support-ticket", help="render and check a support ticket from a Slack request")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--fields", required=True, help="JSON file with the ticket fields")
    sp.add_argument("--title", required=True)
    sp.set_defaults(fn=cmd_support_ticket)

    sp = sub.add_parser("lint-pulse", help="Definition-of-Ready sweep over a Pulse export (JSON list)")
    common(sp)
    sp.add_argument("--export", required=True)
    sp.add_argument("--stage", choices=["draft", "ready"], default="draft")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_lint_pulse)

    sp = sub.add_parser("migrate-store", help="move a project's event log between the text-file and SQLite backends")
    sp.add_argument("project")
    sp.add_argument("--to", required=True, choices=["jsonl", "sqlite"])
    sp.add_argument("--projects-dir", default="projects")
    sp.add_argument("--data-dir", default="data")
    sp.set_defaults(fn=cmd_migrate_store)

    sp = sub.add_parser("assess-call", help="does the call cover what a requirement needs? (no tokens)")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--transcript", help="default: data/<KEY>/<flow>/transcript.txt")
    sp.set_defaults(fn=cmd_assess_call)

    sp = sub.add_parser("call-proceed", help="the delivery manager drafts from a thin call anyway, with a reason")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--by-id", required=True)
    sp.set_defaults(fn=cmd_call_proceed)

    sp = sub.add_parser("models", help="which model size each step runs on; show or change")
    sp.add_argument("project")
    sp.add_argument("--projects-dir", default="projects")
    sp.add_argument("--set", action="append", help="step=tier, e.g. draft_pack=strong (repeatable)")
    sp.add_argument("--escalate-after", type=int, help="failed checks before a step moves up one tier")
    sp.add_argument("--runner-mode", choices=["claude-session", "api"])
    sp.set_defaults(fn=cmd_models)

    sp = sub.add_parser("ai-used", help="record the tokens one model call used")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--action", required=True)
    sp.add_argument("--model", required=True)
    sp.add_argument("--tier")
    sp.add_argument("--input-tokens", type=int, required=True)
    sp.add_argument("--output-tokens", type=int, required=True)
    sp.add_argument("--actor", default="runner")
    sp.set_defaults(fn=cmd_ai_used)

    sp = sub.add_parser("tokens", help="tokens, relative cost and draft pass rate by step and model")
    common(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_tokens)

    sp = sub.add_parser("tick", help="run the code-only steps now; report the model sessions that are still needed")
    common(sp)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--launch", action="store_true", help="EXPERIMENTAL: start one headless `claude -p --model ...` per model")
    sp.set_defaults(fn=cmd_tick)

    sp = sub.add_parser("build-brief", help="assemble the build brief from the documented pack and tickets")
    common(sp)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--pack", required=True)
    sp.add_argument("--tickets", required=True)
    sp.add_argument("--branch")
    sp.add_argument("--tier")
    sp.add_argument("--rules-text", help="file holding the repo's CLAUDE.md content")
    sp.add_argument("--out")
    sp.set_defaults(fn=cmd_build_brief)
