"""The governance dashboard: one self-contained HTML page generated from the event log. No scripts, no external
requests, so it can be emailed, opened offline, or served by `dcp serve`. Every figure is computed from events, and the
page says at the top whether the log passes its integrity check."""
from __future__ import annotations

import html
from datetime import datetime
from typing import List, Optional

from . import tokens as tok
from .events import now_utc
from .flowstate import next_orders
from .scoring import CHECKS, correlate, evaluate_store

h = html.escape

STAGE_OF = [("release.demoed", None, "released"), ("pr.merged", None, "merged"), ("pr.opened", None, "in review"),
            ("build.started", None, "building"), ("branch.shared", None, "branch shared"),
            ("gate.approved", "2", "validated"), ("tickets.created", None, "tickets in Pulse"),
            ("gate.approved", "1", "pack confirmed"), ("pack.linted", None, "pack checked"), ("pack.drafted", None, "pack drafted"),
            ("call.assessed", None, "call assessed"), ("call.recorded", None, "call recorded"),
            ("intake.received", None, "request received"), ("ticket.created", None, "ticket created")]

CSS = """
:root{--bg:#FDF5EC;--surface:#fff;--surface2:#F7EDE2;--line:#E1CFC1;--fg:#1C1C1C;--muted:#6A605A;--accent:#CB161C;
--ok:#2E7D4F;--warn:#9A6200;--bad:#B3261E;--info:#2F5D8A;--ok-bg:#E4F2E9;--warn-bg:#FBEFD5;--bad-bg:#FBE3E1;--info-bg:#E3EDF7;
--sans:'Poppins',system-ui,-apple-system,'Segoe UI',sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#1C1C1C;--surface:#262321;--surface2:#2F2B28;--line:#433C37;--fg:#FDF5EC;
--muted:#B5A99F;--accent:#F25A5F;--ok:#6CC08B;--warn:#E0A84A;--bad:#FF8A80;--info:#8DB8E8;--ok-bg:#1F3328;--warn-bg:#3A2F17;--bad-bg:#3E2220;--info-bg:#1E2D3D;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 var(--sans);padding:20px max(16px,env(safe-area-inset-left)) 56px}
.wrap{max-width:1240px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.sub{color:var(--muted);font-size:13px}.banner{padding:10px 14px;border-radius:6px;margin:12px 0;font-size:13px;border:1px solid var(--line)}
.banner.bad{background:var(--bad-bg);border-color:transparent}.banner.ok{background:var(--ok-bg);border-color:transparent}.banner.warn{background:var(--warn-bg);border-color:transparent}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:14px 0}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px}.tile b{display:block;font-size:24px;font-variant-numeric:tabular-nums}
.tile span{font-size:12px;color:var(--muted)}.scroll{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:640px}th{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);text-align:left;padding:8px 10px;white-space:nowrap}
td{padding:7px 10px;border-top:1px solid var(--line);vertical-align:top;font-variant-numeric:tabular-nums}.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;font-weight:500;white-space:nowrap}
.pass{background:var(--ok-bg);color:var(--ok)}.fail{background:var(--bad-bg);color:var(--bad)}.pending{background:var(--info-bg);color:var(--info)}.na{background:var(--surface2);color:var(--muted)}.warn{background:var(--warn-bg);color:var(--warn)}
.heat td,.heat th{padding:4px 6px;text-align:center}.heat td:first-child,.heat th:first-child{text-align:left;white-space:nowrap}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
.muted{color:var(--muted)}p{margin:6px 0}
"""


def _stage(evs) -> str:
    for type_, gate, label in STAGE_OF:
        for e in reversed(evs):
            if e.type == type_ and (gate is None or str(e.data.get("gate")) == gate) and (type_ != "pack.linted" or e.data.get("passed")):
                return label
    return "-"


def _pill(status: str, text: Optional[str] = None) -> str:
    return f'<span class="pill {h(status)}">{h(text or status)}</span>'


def _score_pill(score: Optional[float]) -> str:
    if score is None:
        return _pill("na", "no data yet")
    return _pill("pass" if score >= 80 else "warn" if score >= 60 else "fail", f"{score:.0f}%")


def _fmt(v, unit="") -> str:
    return "-" if v is None else f"{v}{unit}"


def render_dashboard(cfg: dict, store, rules: dict, now: Optional[datetime] = None, label: str = "") -> str:
    now = now or now_utc()
    key = cfg["key"]
    reports = evaluate_store(store, rules, project=key, now=now)
    orders, waiting = next_orders(store, cfg, now, rules)
    events = store.events(project=key)
    problems = store.verify()
    tk = tok.summarize(events, rules)
    base = rules.get("baselines", {})
    scored = [r for r in reports if r.score is not None]
    avg = round(sum(r.score for r in scored) / len(scored), 1) if scored else None
    people_wait = [w for w in waiting if not w.optional]

    out: List[str] = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                      f"<title>Delivery governance {h(key)}</title><style>{CSS}</style></head><body><div class='wrap'>"]
    out.append(f"<h1>Delivery governance: {h(cfg.get('name', key))}</h1>")
    out.append(f"<div class='sub'>Project {h(key)} ({h(cfg['track'])} track). Computed from the event log at {now.strftime('%Y-%m-%d %H:%M')} UTC. "
               "Every figure traces to events; nothing here is typed in by hand.</div>")
    if label:
        out.append(f"<div class='banner warn'><b>{h(label)}</b></div>")
    if problems:
        out.append("<div class='banner bad'><b>The event log failed its integrity check.</b> Something was edited or removed after the fact. "
                   "Do not trust these figures until it is explained.<br>" + "<br>".join(h(p) for p in problems[:5]) + "</div>")
    else:
        out.append("<div class='banner ok'>Event log integrity check: <b>intact</b> (no event has been edited or removed).</div>")

    out.append("<div class='tiles'>")
    for big, small in ((len(reports), "flows"), (_fmt(avg, "%"), "average compliance"), (len(people_wait), "steps waiting on a person"),
                       (sum(r.metrics["rework_events"] for r in reports), "rework events"),
                       (f"{tk['total']['tokens']:,}" if tk["total"]["calls"] else "-", "AI tokens used")):
        out.append(f"<div class='tile'><b>{h(str(big))}</b><span>{h(small)}</span></div>")
    out.append("</div>")

    # flows
    out.append("<h2>Flows</h2>")
    if not reports:
        out.append("<p class='muted'>No flows yet. They appear when a call is pasted or a support request arrives.</p>")
    else:
        out.append("<div class='scroll'><table><thead><tr><th>Flow</th><th>Track</th><th>Stage</th><th>Compliance</th><th>Waiting on</th>"
                   "<th>Call to pack</th><th>Lead time</th><th>Rework</th><th>Hours vs estimate</th><th>Tokens</th></tr></thead><tbody>")
        for r in reports:
            ws = [w for w in people_wait if w.flow == r.flow]
            m = r.metrics
            out.append(f"<tr><td class='mono'>{h(r.flow)}</td><td>{h(r.track)}</td><td>{h(_stage(store.events(flow=r.flow)))}</td><td>{_score_pill(r.score)}</td>"
                       f"<td>{h(', '.join(sorted({w.owner for w in ws})) or '-')}</td><td>{h(_fmt(m['call_to_pack_min'], ' min'))}</td>"
                       f"<td>{h(_fmt(m['lead_time_days'], ' d'))}</td><td>{m['rework_events']}</td><td>{h(_fmt(m['overrun_pct'], '%'))}</td>"
                       f"<td>{h(_fmt(m['tokens_total']))}</td></tr>")
        out.append("</tbody></table></div>")

        # heatmap
        out.append("<h2>Every check, every flow</h2><p class='sub'>pass, fail, not due yet (...), or not applicable (-). A star means the result is disputed.</p>")
        out.append("<div class='scroll'><table class='heat'><thead><tr><th>Check</th>" + "".join(f"<th class='mono'>{h(r.flow.split('-', 1)[-1])}</th>" for r in reports) + "</tr></thead><tbody>")
        for cid in sorted(CHECKS):
            name = CHECKS[cid][0]
            if not any(c.id == cid for c in reports[0].checks):
                continue
            cells = []
            for r in reports:
                c = next((x for x in r.checks if x.id == cid), None)
                if not c:
                    cells.append("<td></td>")
                    continue
                sym = {"pass": "pass", "fail": "fail", "pending": "...", "na": "-"}[c.status]
                star = "*" if c.disputed else ""
                cells.append(f"<td title='{h(c.detail)}'>{_pill(c.status, sym + star)}</td>")
            out.append(f"<tr><td><span class='mono'>{h(cid)}</span> {h(name)}</td>{''.join(cells)}</tr>")
        out.append("</tbody></table></div><p class='sub'>Hover a cell for the evidence behind it.</p>")

    # who is waiting
    out.append("<h2>Waiting on people</h2>")
    if not waiting:
        out.append("<p class='muted'>Nobody is being waited on.</p>")
    else:
        out.append("<div class='scroll'><table><thead><tr><th>Flow</th><th>Waiting for</th><th>Who</th><th>Since</th><th></th></tr></thead><tbody>")
        for w in waiting:
            since = w.since.strftime("%Y-%m-%d %H:%M") if w.since else "-"
            out.append(f"<tr><td class='mono'>{h(w.flow)}</td><td>{h(w.waiting_for)}</td><td>{h(w.owner)}</td><td>{h(since)}</td>"
                       f"<td class='muted'>{h(w.detail)}{' (optional)' if w.optional else ''}</td></tr>")
        out.append("</tbody></table></div>")

    # speed
    done = [r for r in reports if r.metrics["call_to_pack_min"] is not None]
    if done:
        a = round(sum(r.metrics["call_to_pack_min"] for r in done) / len(done), 1)
        t = [r.metrics["pack_to_tickets_min"] for r in reports if r.metrics["pack_to_tickets_min"] is not None]
        out.append("<h2>Speed against the playbook</h2><div class='scroll'><table><thead><tr><th>Step</th><th>Average</th><th>Playbook says</th><th>Flows</th></tr></thead><tbody>")
        out.append(f"<tr><td>Call to requirement pack</td><td>{a} min</td><td>{h(str(base.get('call_to_pack_minutes', '-')))} min</td><td>{len(done)}</td></tr>")
        if t:
            out.append(f"<tr><td>Gate 1 to tickets in Pulse</td><td>{round(sum(t) / len(t), 1)} min</td><td>{h(str(base.get('pack_to_tickets_minutes', '-')))} min</td><td>{len(t)}</td></tr>")
        out.append("</tbody></table></div><p class='sub'>The playbook's figures are its claims. Replace them with a measured baseline from before rollout in config/rules.json.</p>")

    # mapping
    out.append("<h2>Does following the process pay off?</h2>")
    rows = []
    for metric, name in (("rework_events", "Rework events"), ("overrun_pct", "Hours over estimate (%)"), ("lead_time_days", "Lead time (days)"), ("tokens_total", "AI tokens")):
        c = correlate(reports, rules, metric)
        if "spearman" in c:
            rows.append(f"<tr><td>{h(name)}</td><td>{c['n']}</td><td>{_fmt(c['spearman'])}</td><td>{_fmt(c['high_compliance_avg'])}</td><td>{_fmt(c['low_compliance_avg'])}</td><td class='muted'>{h(c['note'])}</td></tr>")
    if rows:
        out.append("<div class='scroll'><table><thead><tr><th>Outcome</th><th>Flows</th><th>Rank correlation with compliance</th><th>Average at 80%+</th><th>Average below 80%</th><th>Read this as</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
        out.append("<p class='sub'>A negative correlation means higher compliance goes with less of the outcome. Correlation is not proof: compare against the pre-rollout baseline before claiming a gain.</p>")
    else:
        out.append("<p class='muted'>Not enough finished flows with outcome data yet. Import hours and rework with <span class='mono'>dcp import-hours</span> and <span class='mono'>dcp import-rework</span>.</p>")

    # disputes
    disputes = [e for e in events if e.type in ("score.disputed", "score.dispute_resolved")]
    if disputes:
        out.append("<h2>Disputed scores</h2><div class='scroll'><table><thead><tr><th>When</th><th>Flow</th><th>Check</th><th>What happened</th></tr></thead><tbody>")
        for e in disputes:
            what = (f"disputed by {e.actor}: {e.data['reason']}" if e.type == "score.disputed" else f"{e.data['outcome']} by {e.actor}: {e.data['note']}")
            out.append(f"<tr><td>{e.ts.strftime('%Y-%m-%d')}</td><td class='mono'>{h(e.flow)}</td><td class='mono'>{h(e.data['check'])}</td><td>{h(what)}</td></tr>")
        out.append("</tbody></table></div>")

    # tokens
    if tk["total"]["calls"]:
        out.append("<h2>AI use</h2><div class='scroll'><table><thead><tr><th>Step</th><th>Calls</th><th>Average tokens</th><th>Tokens</th><th>Relative cost</th></tr></thead><tbody>")
        for a, b in sorted(tk["by_action"].items(), key=lambda kv: -(kv[1]["input"] + kv[1]["output"])):
            out.append(f"<tr><td>{h(a)}</td><td>{b['calls']}</td><td>{b['avg_tokens_per_call']:,}</td><td>{b['input'] + b['output']:,}</td><td>{b['cost_units']}</td></tr>")
        out.append("</tbody></table></div><p class='sub'>Relative cost uses placeholder weights in config/rules.json. Replace them with real prices before reading cost.</p>")
        oc = tok.outcomes(events)
        if oc:
            out.append("<div class='scroll'><table><thead><tr><th>Model</th><th>Drafts</th><th>Accepted by the checks</th><th>First try</th></tr></thead><tbody>"
                       + "".join(f"<tr><td>{h(m)}</td><td>{b['drafts']}</td><td>{b['passed']}</td><td>{_fmt(b['first_try_pass_rate'], '%')}</td></tr>" for m, b in oc.items())
                       + "</tbody></table></div>")

    out.append("<p class='sub' style='margin-top:28px'>Scores are for coaching and for the performance conversation, with the evidence beside every number. "
               "A person can dispute any check; a practice lead decides, never the person being scored.</p></div></body></html>")
    return "".join(out)
