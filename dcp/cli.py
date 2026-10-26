from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from .config import load_rules
from .drift import check_drift
from .events import EventError, open_store, parse_ts
from .packlint import lint_pack
from .scoring import PASS, FAIL, PENDING, correlate, evaluate_store
from .ticketlint import lint_tickets


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _emit(result, args) -> int:
    print(json.dumps(result.to_dict(), indent=2) if args.json else result.render())
    return 0 if result.passed else 1


def _maybe_log(args, type_: str, data: dict) -> None:
    if getattr(args, "log_to", None):
        for need in ("flow", "project", "actor"):
            if not getattr(args, need, None):
                sys.exit(f"--log-to needs --{need}")
        open_store(args.log_to).append(type_, args.flow, args.project, args.actor, data, track=args.track)


def cmd_lint_pack(args) -> int:
    rules = load_rules(args.rules)
    res = lint_pack(_read(args.pack), _read(args.transcript) if args.transcript else None, rules, args.pack)
    _maybe_log(args, "pack.linted", {"version": args.version, "passed": res.passed, "blocks": res.blocks,
                                     "warns": res.warns, "open_questions": res.stats.get("open_questions", 0)})
    return _emit(res, args)


def cmd_lint_tickets(args) -> int:
    rules = load_rules(args.rules)
    today = date.fromisoformat(args.today) if args.today else None
    if args.size_optional:
        rules = json.loads(json.dumps(rules))
        rules["lint"].setdefault("severity_overrides", {})["TK014"] = "warn"
    res = lint_tickets(_read(args.tickets), rules, args.stage, today, args.tickets)
    _maybe_log(args, "tickets.linted", {"passed": res.passed, "blocks": res.blocks, "warns": res.warns,
                                        "stage": args.stage,
                                        "owner_date_gaps": res.stats.get("owner_date_gaps", 0)})
    return _emit(res, args)


def cmd_drift(args) -> int:
    rules = load_rules(args.rules)
    res = check_drift(_read(args.pack), _read(args.tickets), rules)
    _maybe_log(args, "drift.checked", {"passed": res.passed, "blocks": res.blocks})
    return _emit(res, args)


def cmd_log(args) -> int:
    try:
        data = json.loads(args.data) if args.data else {}
        ev = open_store(args.store).append(args.type, args.flow, args.project, args.actor, data,
                                           ts=args.ts, track=args.track)
    except (EventError, json.JSONDecodeError) as e:
        sys.exit(f"error: {e}")
    print(f"logged {ev.type} ({ev.id})")
    return 0


def cmd_log_head(args) -> int:
    n, h = open_store(args.store).head()
    print(f"{n}:{h}" if args.short else f"events: {n}\nhead hash: {h}\nanchor (publish this): {n}:{h}")
    return 0


def cmd_verify(args) -> int:
    store = open_store(args.store)
    problems = store.verify()
    for a in args.anchor or []:
        try:
            n, h = a.split(":", 1)
            why = store.check_anchor(int(n), h)
        except ValueError:
            problems.append(f"anchor '{a}' must look like 29:<hash>")
            continue
        if why:
            problems.append("anchor mismatch: " + why)
    if problems:
        print("LOG TAMPERED OR CORRUPT")
        print("\n".join("  " + p for p in problems))
        return 1
    print("log intact")
    return 0


def _fmt(v) -> str:
    return "-" if v is None else str(v)


def cmd_score(args) -> int:
    rules = load_rules(args.rules)
    now = parse_ts(args.now) if args.now else None
    reports = evaluate_store(open_store(args.store), rules, args.project, args.flow, now)
    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
        return 0
    if not reports:
        print("no flows found")
        return 0
    for r in reports:
        print(f"\n{r.flow}  [{r.project} / {r.track}]  compliance: {_fmt(r.score)}%")
        for c in r.checks:
            if c.status == "na" and not args.all:
                continue
            mark = {PASS: "PASS", FAIL: "FAIL", PENDING: "....", "na": " -- "}[c.status]
            print(f"  {mark} {c.id} {c.name}: {c.detail}")
        m = r.metrics
        print(f"  speed: call>pack {_fmt(m['call_to_pack_min'])} min | pack>tickets {_fmt(m['pack_to_tickets_min'])} min "
              f"| call>build {_fmt(m['call_to_build_days'])} d | lead time {_fmt(m['lead_time_days'])} d")
        print(f"  rework: {m['rework_events']} events | overrun: {_fmt(m['overrun_pct'])}%")
    return 0


def cmd_report(args) -> int:
    rules = load_rules(args.rules)
    reports = evaluate_store(open_store(args.store), rules, args.project, None, parse_ts(args.now) if args.now else None)
    scored = [r for r in reports if r.score is not None]
    out = {
        "flows": len(reports),
        "avg_compliance": round(sum(r.score for r in scored) / len(scored), 1) if scored else None,
        "mapping": [correlate(reports, rules, k) for k in ("rework_events", "overrun_pct", "lead_time_days", "tokens_total")],
    }
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"flows: {out['flows']}   average compliance: {_fmt(out['avg_compliance'])}%")
    for m in out["mapping"]:
        print(f"\ncompliance vs {m['metric']}  (n={m['n']})")
        if "spearman" in m:
            print(f"  rank correlation: {_fmt(m['spearman'])}   "
                  f"high-compliance avg: {_fmt(m['high_compliance_avg'])}   low: {_fmt(m['low_compliance_avg'])}   "
                  f"cohorts: {m['cohort_sizes']}")
        print(f"  {m['note']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dcp", description="Delivery Control Plane")
    p.add_argument("--rules", help="path to rules.json (default: config/rules.json or $DCP_RULES)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def logging_opts(sp):
        sp.add_argument("--log-to", metavar="STORE", help="also append the result to this event log")
        sp.add_argument("--flow")
        sp.add_argument("--project")
        sp.add_argument("--actor")
        sp.add_argument("--track", default="build", choices=["build", "support"])

    sp = sub.add_parser("lint-pack", help="lint a Requirement Pack against its call transcript")
    sp.add_argument("pack")
    sp.add_argument("--transcript")
    sp.add_argument("--version", type=int, default=1)
    sp.add_argument("--json", action="store_true")
    logging_opts(sp)
    sp.set_defaults(fn=cmd_lint_pack)

    sp = sub.add_parser("lint-tickets", help="check a ticket set against the Definition of Ready")
    sp.add_argument("tickets")
    sp.add_argument("--stage", choices=["draft", "ready"], default="draft")
    sp.add_argument("--today", help="override today's date (YYYY-MM-DD)")
    sp.add_argument("--size-optional", action="store_true",
                    help="a missing tech-lead size is a warning (use when the tech lead does not have to validate)")
    sp.add_argument("--json", action="store_true")
    logging_opts(sp)
    sp.set_defaults(fn=cmd_lint_tickets)

    sp = sub.add_parser("check-drift", help="compare the confirmed pack with the ticket set")
    sp.add_argument("pack")
    sp.add_argument("tickets")
    sp.add_argument("--json", action="store_true")
    logging_opts(sp)
    sp.set_defaults(fn=cmd_drift)

    sp = sub.add_parser("log", help="append one event to the log")
    sp.add_argument("type")
    sp.add_argument("--store", required=True)
    sp.add_argument("--flow", required=True)
    sp.add_argument("--project", required=True)
    sp.add_argument("--actor", required=True)
    sp.add_argument("--track", default="build", choices=["build", "support"])
    sp.add_argument("--data", help="JSON object")
    sp.add_argument("--ts", help="ISO timestamp (default: now)")
    sp.set_defaults(fn=cmd_log)

    sp = sub.add_parser("log-head", help="the log's size and last hash: publish it daily so a full rewrite is detectable")
    sp.add_argument("store")
    sp.add_argument("--short", action="store_true")
    sp.set_defaults(fn=cmd_log_head)

    sp = sub.add_parser("verify-log", help="check the event log has not been altered")
    sp.add_argument("store")
    sp.add_argument("--anchor", action="append", help="a published 'count:hash'; the log must still contain it (repeatable)")
    sp.set_defaults(fn=cmd_verify)

    sp = sub.add_parser("score", help="compliance score and speed per flow")
    sp.add_argument("store")
    sp.add_argument("--project")
    sp.add_argument("--flow")
    sp.add_argument("--now", help="evaluate as of this time (ISO)")
    sp.add_argument("--all", action="store_true", help="also show checks that do not apply")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_score)

    sp = sub.add_parser("report", help="map compliance to rework, overrun and lead time")
    sp.add_argument("store")
    sp.add_argument("--project")
    sp.add_argument("--now")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_report)

    from . import cli_extra, cli_ops
    cli_ops.register(sub)
    cli_extra.register(sub)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
