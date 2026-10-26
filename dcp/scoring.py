"""Compliance scoring and the efficiency mapping, computed purely from the event log.

Status per check: pass | fail | na (does not apply) | pending (not due yet).
Score = weight of passes / weight of (passes + fails). Pending and na never count against anyone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from .events import Event, EventStore, now_utc

PASS, FAIL, NA, PENDING = "pass", "fail", "na", "pending"


@dataclass
class CheckResult:
    id: str
    name: str
    status: str
    detail: str
    weight: int
    evidence: List[str] = field(default_factory=list)
    disputed: Optional[str] = None      # None | open | upheld | overturned


class Ctx:
    def __init__(self, events: List[Event], params: dict, roles: dict, now: datetime):
        self.events = events
        self.params = params
        self.roles = roles
        self.now = now

    def of(self, type: str) -> List[Event]:
        return [e for e in self.events if e.type == type]

    def first(self, type: str) -> Optional[Event]:
        r = self.of(type)
        return r[0] if r else None

    def last(self, type: str) -> Optional[Event]:
        r = self.of(type)
        return r[-1] if r else None

    def gate(self, n: int, type: str) -> Optional[Event]:
        r = [e for e in self.of(type) if str(e.data.get("gate")) == str(n)]
        return r[-1] if r else None

    def pr_head(self, pr) -> Optional[str]:
        shas = [e.data["head_sha"] for e in self.events
                if e.type in ("pr.opened", "pr.pushed") and e.data.get("pr") == pr]
        return shas[-1] if shas else None

    def tier(self) -> Optional[str]:
        """The tier a lead set always wins. A pull request's own declaration is used only when no lead has set one,
        so a developer cannot lower the bar by declaring a lower tier in the PR."""
        for e in reversed(self.events):
            if e.type == "tier.set":
                return str(e.data["tier"]).upper()
        for e in reversed(self.events):
            if e.type == "pr.opened" and e.data.get("tier"):
                return str(e.data["tier"]).upper()
        return None


Result = Tuple[str, str, List[str]]
CHECKS: Dict[str, Tuple[str, Tuple[str, ...], Callable[[Ctx], Result]]] = {}


def check(cid: str, name: str, tracks=("build",)):
    def deco(fn):
        CHECKS[cid] = (name, tuple(tracks), fn)
        return fn
    return deco


def _mins(a: Event, b: Event) -> float:
    return (b.ts - a.ts).total_seconds() / 60.0


@check("C01", "Call note titled 'Project :: topic :: KEY'")
def c01(x: Ctx) -> Result:
    call = x.first("call.recorded")
    if not call:
        return PENDING, "No call recorded yet", []
    latest = x.last("call.retitled") or call
    ok = re.match(x.params.get("title_regex", ".+"), latest.data["title"]) is not None
    return (PASS if ok else FAIL), f"Title: {latest.data['title']!r}", list(dict.fromkeys([call.id, latest.id]))


@check("C02", "Requirement Pack drafted within an hour of the call")
def c02(x: Ctx) -> Result:
    call, pack = x.first("call.recorded"), x.first("pack.drafted")
    if not call:
        return NA, "No call event", []
    limit = x.params.get("max_minutes", 60)
    if not pack:
        if (x.now - call.ts) > timedelta(minutes=limit):
            return FAIL, f"No pack {limit}+ minutes after the call", [call.id]
        return PENDING, "Pack not drafted yet", [call.id]
    m = _mins(call, pack)
    return (PASS if m <= limit else FAIL), f"Pack drafted {m:.0f} min after the call (limit {limit})", [call.id, pack.id]


@check("C03", "Pack passed the lint (traceable, testable, owned)")
def c03(x: Ctx) -> Result:
    lint = x.last("pack.linted")
    if not lint:
        return PENDING, "Pack not linted yet", []
    ok = bool(lint.data["passed"])
    return (PASS if ok else FAIL), f"Latest lint: {lint.data['blocks']} blocking, {lint.data['warns']} warnings", [lint.id]


@check("C04", "Open questions sent to the client the same day")
def c04(x: Ctx) -> Result:
    call, lint = x.first("call.recorded"), x.last("pack.linted")
    if not call or not lint:
        return PENDING, "Waiting for call and pack", []
    if int(lint.data.get("open_questions", 1)) == 0:
        return NA, "Pack has no open questions", [lint.id]
    sent = x.first("questions.sent")
    limit = x.params.get("max_hours", 24)
    if not sent:
        if (x.now - call.ts) > timedelta(hours=limit):
            return FAIL, f"Questions not sent {limit}+ hours after the call", [call.id]
        return PENDING, "Questions not sent yet", [call.id]
    h = (sent.ts - call.ts).total_seconds() / 3600
    return (PASS if h <= limit else FAIL), f"Sent {h:.1f} h after the call (limit {limit})", [call.id, sent.id]


def _gate_check(x: Ctx, n: int, roles_key: str) -> Result:
    opened, ok = x.gate(n, "gate.opened"), x.gate(n, "gate.approved")
    if not ok:
        return PENDING, f"Gate {n} not approved yet", [opened.id] if opened else []
    allowed = x.roles.get(roles_key, [])
    if allowed and ok.data["approver_role"] not in allowed:
        return FAIL, f"Approved by role '{ok.data['approver_role']}', expected one of {allowed}", [ok.id]
    return PASS, f"Approved by {ok.data['approver']} ({ok.data['approver_role']})", [ok.id]


@check("C05", "Gate 1 genuinely reviewed (right approver, not a rubber stamp)")
def c05(x: Ctx) -> Result:
    status, detail, ev = _gate_check(x, 1, "gate1_approvers")
    if status != PASS:
        return status, detail, ev
    opened, ok = x.gate(1, "gate.opened"), x.gate(1, "gate.approved")
    floor = x.params.get("min_review_seconds", 60)
    if opened:
        secs = (ok.ts - opened.ts).total_seconds()
        if secs < floor:
            return FAIL, f"Approved {secs:.0f}s after opening, under the {floor}s floor. Not read.", [opened.id, ok.id]
        return PASS, detail + f"; reviewed for {secs / 60:.1f} min", [opened.id, ok.id]
    return PASS, detail + "; no open event, review time unknown", ev


@check("C06", "Ticket set meets Definition of Ready")
def c06(x: Ctx) -> Result:
    lint = [e for e in x.of("tickets.linted") if e.data.get("stage", "ready") == "ready"]
    if not lint:
        return PENDING, "Ready-stage ticket lint not run yet", []
    ok = bool(lint[-1].data["passed"])
    return (PASS if ok else FAIL), f"{lint[-1].data['blocks']} blocking findings at ready stage", [lint[-1].id]


@check("C07", "Every dependency and risk has an owner and a date")
def c07(x: Ctx) -> Result:
    lint = x.last("tickets.linted")
    if not lint:
        return PENDING, "No ticket lint yet", []
    gaps = int(lint.data.get("owner_date_gaps", 0))
    return (PASS if gaps == 0 else FAIL), f"{gaps} owner/date gaps", [lint.id]


@check("C08", "Tickets match the confirmed pack (no drift)")
def c08(x: Ctx) -> Result:
    d = x.last("drift.checked")
    if not d:
        return PENDING, "Drift check not run yet", []
    return (PASS if d.data["passed"] else FAIL), f"{d.data['blocks']} drift findings", [d.id]


@check("C09", "Gate 2 validated before development (delivery manager; tech lead too when required)")
def c09(x: Ctx) -> Result:
    opened = x.gate(2, "gate.opened")
    required = list((opened.data.get("required_roles") if opened else None) or ["delivery_manager"])
    approvals = [e for e in x.of("gate.approved") if str(e.data.get("gate")) == "2"]
    if not approvals:
        return PENDING, "Gate 2 not validated yet", [opened.id] if opened else []
    allowed = x.roles.get("gate2_approvers", [])
    wrong = [e for e in approvals if allowed and e.data["approver_role"] not in allowed]
    if wrong:
        return FAIL, f"Validated by role '{wrong[0].data['approver_role']}', expected one of {allowed}", [wrong[0].id]
    have = {e.data["approver_role"] for e in approvals}
    missing = [r for r in required if r not in have]
    if missing:
        return PENDING, f"Still needs validation from {', '.join(missing)}", [e.id for e in approvals]
    return PASS, "Validated by " + ", ".join(sorted(have)), [e.id for e in approvals]


@check("C10", "Claude Code assumptions note came back to the pack", ("build",))
def c10(x: Ctx) -> Result:
    start = x.first("build.started")
    if not start:
        return PENDING, "Build not started", []
    note = x.first("assumptions.received")
    if note:
        return PASS, "Assumptions note recorded", [start.id, note.id]
    grace = x.params.get("grace_days", 3)
    if (x.now - start.ts) > timedelta(days=grace):
        return FAIL, f"No assumptions note {grace}+ days after build start", [start.id]
    return PENDING, "Waiting for the assumptions note", [start.id]


@check("C11", "Every PR links its ticket", ("build", "support"))
def c11(x: Ctx) -> Result:
    prs = x.of("pr.opened")
    if not prs:
        return PENDING, "No PR yet", []
    bad = [e for e in prs if not e.data.get("ticket")]
    return (FAIL if bad else PASS), (f"{len(bad)} PR(s) without a ticket" if bad else "All PRs link a ticket"), [e.id for e in (bad or prs)]


@check("C12", "AI use declared on every PR", ("build", "support"))
def c12(x: Ctx) -> Result:
    prs = x.of("pr.opened")
    if not prs:
        return PENDING, "No PR yet", []
    bad = [e for e in prs if not e.data.get("ai_declared")]
    return (FAIL if bad else PASS), (f"{len(bad)} PR(s) without the AI declaration" if bad else "Declared"), [e.id for e in (bad or prs)]


@check("C13", "Parakh verdict is current and clean", ("build", "support"))
def c13(x: Ctx) -> Result:
    prs = x.of("pr.opened")
    if not prs:
        return PENDING, "No PR yet", []
    if x.tier() == "T3":
        return NA, "T3 change: Parakh optional", []
    problems, ev = [], []
    for pr in prs:
        pid = pr.data["pr"]
        head = x.pr_head(pid)
        verdicts = [v for v in x.of("parakh.verdict") if v.data["pr"] == pid and v.data["head_sha"] == head]
        if not verdicts:
            problems.append(f"PR {pid}: no Parakh verdict for the latest commit {str(head)[:7]}")
        elif str(verdicts[-1].data["gate"]).lower() != "pass":
            problems.append(f"PR {pid}: Parakh gate is {verdicts[-1].data['gate']}")
            ev.append(verdicts[-1].id)
        else:
            ev.append(verdicts[-1].id)
    return (FAIL if problems else PASS), ("; ".join(problems) or "Clean verdict on the latest commit"), ev


@check("C14", "Maker is not the checker; T1 had a senior approver", ("build", "support"))
def c14(x: Ctx) -> Result:
    merges = x.of("pr.merged")
    if not merges:
        return PENDING, "Nothing merged yet", []
    for m in merges:
        opened = [e for e in x.of("pr.opened") if e.data["pr"] == m.data["pr"]]
        author = opened[0].data["author"] if opened else None
        approvers = m.data.get("approvers") or []
        if not approvers:
            return FAIL, f"PR {m.data['pr']} merged with no approver", [m.id]
        if author and author in approvers:
            return FAIL, f"PR {m.data['pr']} approved by its own author", [m.id]
        if x.tier() == "T1" and not m.data.get("senior_approved"):
            return FAIL, f"PR {m.data['pr']} is T1 but no senior approval recorded", [m.id]
    return PASS, "Independent approval on every merge", [m.id for m in merges]


@check("C15", "Merge decided by the tech lead (Gate 3)")
def c15(x: Ctx) -> Result:
    merges = x.of("pr.merged")
    if not merges:
        return PENDING, "Nothing merged yet", []
    allowed = x.roles.get("gate3_approvers", [])
    bad = [m for m in merges if allowed and m.data["merger_role"] not in allowed]
    return (FAIL if bad else PASS), (f"{len(bad)} merge(s) not by {allowed}" if bad else "Merged by tech lead"), [m.id for m in (bad or merges)]


@check("C16", "Risk tier set by a lead before work started", ("build", "support"))
def c16(x: Ctx) -> Result:
    t = x.last("tier.set")
    started = x.first("build.started") or x.first("pr.opened")
    if not started and not t:
        return PENDING, "Nothing started yet", []
    if not t:
        return FAIL, "Work started with no tier set", [started.id] if started else []
    if x.roles.get("tier_setters") and t.data["actor_role"] not in x.roles["tier_setters"]:
        return FAIL, f"Tier set by '{t.data['actor_role']}', leads must set it", [t.id]
    if started and t.ts > started.ts:
        return FAIL, "Tier set after work started", [t.id, started.id]
    return PASS, f"Tier {t.data['tier']} set by {t.actor}", [t.id]


@check("C17", "T1 evidence present (failure-path test and rollback note)", ("build", "support"))
def c17(x: Ctx) -> Result:
    if x.tier() != "T1":
        return NA, "Not a T1 change", []
    prs = x.of("pr.opened")
    if not prs:
        return PENDING, "No PR yet", []
    bad = [p for p in prs if not (p.data.get("failure_path_test") and p.data.get("rollback_note"))]
    return (FAIL if bad else PASS), (f"{len(bad)} T1 PR(s) missing test or rollback note" if bad else "Both present"), [p.id for p in (bad or prs)]


@check("C21", "Branch follows GitHub flow and the pull request targets the base branch", ("build", "support"))
def c21(x: Ctx) -> Result:
    prs = x.of("pr.opened")
    if not prs:
        return PENDING, "No PR yet", []
    bad = [p for p in prs if p.data.get("branch_problem")]
    return (FAIL if bad else PASS), (bad[0].data["branch_problem"] if bad else "Branch and target follow the strategy"), [p.id for p in (bad or prs)]


@check("C20", "Stories sized by the tech lead before development (informational, weight 0)")
def c20(x: Ctx) -> Result:
    if not x.first("tickets.created"):
        return PENDING, "Tickets not created yet", []
    sized, start = x.last("tech.sized"), x.first("build.started")
    if sized and (not start or sized.ts <= start.ts):
        return PASS, "Sized by " + sized.actor, [sized.id]
    if start:
        return FAIL, "Development started with no tech-lead sizes, so estimate vs actual cannot be measured", [start.id]
    return PENDING, "Not sized yet (optional, any time before development)", []


@check("C18", "Release demoed against the acceptance criteria")
def c18(x: Ctx) -> Result:
    if not x.of("pr.merged"):
        return PENDING, "Nothing merged yet", []
    d = x.last("release.demoed")
    if not d:
        return PENDING, "Demo not recorded", []
    return (PASS if d.data["against_criteria"] else FAIL), "Demo " + ("against criteria" if d.data["against_criteria"] else "not tied to criteria"), [d.id]


@check("C19", "Break-glass use reviewed afterwards", ("build", "support"))
def c19(x: Ctx) -> Result:
    used = x.of("breakglass.used")
    if not used:
        return NA, "No break-glass use", []
    limit = x.params.get("review_within_hours", 24)
    reviewed = x.of("breakglass.reviewed")
    for u in used:
        r = [e for e in reviewed if e.ts >= u.ts]
        if not r:
            if (x.now - u.ts) > timedelta(hours=limit):
                return FAIL, f"Break-glass ('{u.data['reason']}') never reviewed", [u.id]
            return PENDING, "Break-glass review pending", [u.id]
        if (r[0].ts - u.ts) > timedelta(hours=limit):
            return FAIL, f"Reviewed after {limit}h", [u.id, r[0].id]
    return PASS, "Reviewed in time", [u.id for u in used]


# ---------- per-flow report ----------

@dataclass
class FlowReport:
    flow: str
    project: str
    track: str
    score: Optional[float]
    checks: List[CheckResult]
    metrics: dict

    def to_dict(self) -> dict:
        return {"flow": self.flow, "project": self.project, "track": self.track, "score": self.score,
                "metrics": self.metrics, "checks": [c.__dict__ for c in self.checks]}


def flow_metrics(x: Ctx) -> dict:
    ev = x.events
    call, pack = x.first("call.recorded"), x.first("pack.drafted")
    g1, tix = x.gate(1, "gate.approved"), x.first("tickets.created")
    start = x.first("build.started")
    merged = x.first("pr.merged")

    def mins(a, b):
        return round(_mins(a, b), 1) if a and b else None

    hours = x.last("hours.recorded")
    est, act = (hours.data["estimate_hours"], hours.data["actual_hours"]) if hours else (None, None)
    overrun = round((act - est) / est * 100, 1) if est and act is not None else None
    defects = [e for e in x.of("defect.found")]
    m = {
        "call_to_pack_min": mins(call, pack),
        "pack_to_tickets_min": mins(g1, tix),
        "call_to_build_days": round(_mins(call, start) / 1440, 2) if call and start else None,
        "lead_time_days": round(_mins(call, merged) / 1440, 2) if call and merged else None,
        "reopened_tickets": len(x.of("ticket.reopened")),
        "change_requests": len(x.of("change_request.raised")),
        "defects_uat_or_prod": sum(1 for d in defects if d.data["phase"] in ("uat", "prod")),
        "assumption_gaps_late": sum(1 for g in x.of("assumption_gap.found") if g.data["phase"] in ("uat", "prod")),
        "estimate_hours": est, "actual_hours": act, "overrun_pct": overrun,
    }
    ai = x.of("ai.used")
    m["ai_calls"] = len(ai)
    m["tokens_total"] = sum(int(e.data["input_tokens"]) + int(e.data["output_tokens"]) for e in ai) or None
    m["rework_events"] = (m["reopened_tickets"] + m["change_requests"]
                          + m["defects_uat_or_prod"] + m["assumption_gaps_late"])
    return m


def evaluate_flow(events: List[Event], rules: dict, now: Optional[datetime] = None) -> FlowReport:
    now = now or now_utc()
    comp = rules["compliance"]
    track = events[0].track if events else "build"
    results: List[CheckResult] = []
    for cid, (name, tracks, fn) in CHECKS.items():
        conf = comp["checks"].get(cid, {"enabled": True, "weight": 1, "params": {}})
        if not conf.get("enabled", True):
            continue
        weight = int(conf.get("weight", 1))
        if track not in tracks:
            results.append(CheckResult(cid, name, NA, f"Not part of the {track} track", weight))
            continue
        status, detail, evidence = fn(Ctx(events, conf.get("params", {}), comp.get("roles", {}), now))
        results.append(CheckResult(cid, name, status, detail, weight, evidence))
    disputes: Dict[str, dict] = {}
    for e in events:
        if e.type == "score.disputed":
            disputes[e.data["check"]] = {"state": "open", "by": e.actor}
        elif e.type == "score.dispute_resolved":
            disputes[e.data["check"]] = {"state": e.data["outcome"], "note": e.data["note"]}
    for r in results:
        d = disputes.get(r.id)
        if not d:
            continue
        r.disputed = d["state"]
        if d["state"] == "overturned":
            r.detail = f"Overturned on dispute ({d['note']}). Was: {r.status}. {r.detail}"
            r.status = NA
    earned = sum(r.weight for r in results if r.status == PASS)
    due = sum(r.weight for r in results if r.status in (PASS, FAIL))
    score = round(earned / due * 100, 1) if due else None
    x = Ctx(events, {}, comp.get("roles", {}), now)
    project = events[0].project if events else ""
    return FlowReport(events[0].flow if events else "", project, track, score, results, flow_metrics(x))


def evaluate_store(store: EventStore, rules: dict, project: Optional[str] = None,
                   flow: Optional[str] = None, now: Optional[datetime] = None) -> List[FlowReport]:
    reports = []
    for f in ([flow] if flow else store.flows(project)):
        evs = store.events(flow=f)
        if evs:
            reports.append(evaluate_flow(evs, rules, now))
    return reports


# ---------- mapping compliance to efficiency ----------

def _ranks(vals: List[float]) -> List[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((a - mx) ** 2 for a in xs) ** 0.5
    sy = sum((b - my) ** 2 for b in ys) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (sx * sy)


def correlate(reports: List[FlowReport], rules: dict, y_key: str) -> dict:
    """How does compliance relate to an outcome (rework_events, overrun_pct, lead_time_days)?"""
    base = rules.get("baselines", {})
    min_n = base.get("min_flows_for_correlation", 8)
    thr = base.get("high_compliance_threshold", 80)
    pts = [(r.score, r.metrics.get(y_key)) for r in reports if r.score is not None and r.metrics.get(y_key) is not None]
    out = {"metric": y_key, "n": len(pts)}
    if len(pts) < 3:
        out["note"] = "Too few completed flows to say anything."
        return out
    xs, ys = [p[0] for p in pts], [float(p[1]) for p in pts]
    out["spearman"] = None
    rho = _pearson(_ranks(xs), _ranks(ys))
    out["spearman"] = round(rho, 2) if rho is not None else None
    hi = [y for s, y in pts if s >= thr]
    lo = [y for s, y in pts if s < thr]
    out["high_compliance_avg"] = round(sum(hi) / len(hi), 2) if hi else None
    out["low_compliance_avg"] = round(sum(lo) / len(lo), 2) if lo else None
    out["cohort_sizes"] = {"high": len(hi), "low": len(lo)}
    out["note"] = ("Indicative only: too few flows for a reliable read." if len(pts) < min_n
                   else "Enough flows for a rough read. Correlation is not proof; compare against the "
                        "pre-rollout baseline before claiming a gain.")
    return out
