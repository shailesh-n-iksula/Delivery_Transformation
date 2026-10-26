"""What is due, per project, right now. Pure function of (event log, project config, clock).

Two kinds of output:
  orders  : things the system does next (draft, lint, post to Slack, create tickets, launch a build)
            and timed polls (new Granola notes, Slack replies, Pulse sweep).
  waiting : steps that only a person can do, with how long they have been waiting and who.

A runner (a scheduled Claude session with the Granola, Slack and Pulse connectors) calls `dcp next`,
does the orders, logs what happened, and repeats. The log is the only state, so a crashed or
repeated run cannot skip or double a step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .events import Event, EventStore, now_utc
from .models import assign as assign_model

MAX_PACK_ATTEMPTS = 3
T1_WORDS = ("payment", "checkout", "order", "oms", "erp", "sync", "auth", "otp", "login", "password", "pii",
            "patch", "vulnerab", "security", "cve", "encryption", "credential", "token", "config", "infra",
            "refund", "invoice", "stock", "inventory", "sap")
T3_WORDS = ("css", "copy", "typo", "text change", "wording", "docs", "banner", "colour", "color", "icon")


def suggest_tier(text: str) -> str:
    """A suggestion only. Leads set the tier (Parakh rule); this saves them typing."""
    t = text.lower()
    if any(w in t for w in T1_WORDS):
        return "T1"
    if any(w in t for w in T3_WORDS):
        return "T3"
    return "T2"


@dataclass
class Order:
    id: str
    flow: str
    action: str
    why: str
    kind: str = "task"          # task | poll
    inputs: dict = field(default_factory=dict)
    step_kind: str = ""         # code | connector | ai  (see models.py)
    tier: str = "none"          # none | small | standard | strong
    model: Optional[str] = None  # alias for the tier, e.g. 'sonnet'; None = no model
    needs_ai: bool = False      # True only for drafting steps

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Waiting:
    flow: str
    waiting_for: str
    owner: str                  # role: delivery_manager | tech_lead | client | developer
    since: Optional[datetime]
    detail: str = ""
    optional: bool = False      # visible, never nudged, never blocks

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["since"] = self.since.strftime("%Y-%m-%dT%H:%M:%SZ") if self.since else None
        return d


def _last(evs: List[Event], type_: str, pred=None) -> Optional[Event]:
    for e in reversed(evs):
        if e.type == type_ and (pred is None or pred(e)):
            return e
    return None


def _all(evs: List[Event], type_: str, pred=None) -> List[Event]:
    return [e for e in evs if e.type == type_ and (pred is None or pred(e))]


def _gate(evs, n, type_):
    return _last(evs, type_, lambda e: str(e.data.get("gate")) == str(n))


def _due(poll_events: List[Event], stage: str, minutes: int, now: datetime) -> bool:
    last = _last(poll_events, "poll.ran", lambda e: e.data.get("stage") == stage)
    return last is None or (now - last.ts) >= timedelta(minutes=minutes)


def can_approve(evs: List[Event], gate: int) -> List[str]:
    """Reasons a gate cannot be approved yet. Empty list = approvable. This is the hard stop."""
    reasons: List[str] = []
    if gate in (1, 2):
        evs = epoch_events(evs)
    if gate == 1:
        packs = _all(evs, "pack.drafted")
        if not packs:
            return ["no Requirement Pack has been drafted"]
        v = max(p.data["version"] for p in packs)
        lint = _last(evs, "pack.linted", lambda e: e.data["version"] == v)
        if not lint:
            reasons.append(f"pack v{v} has not been linted")
        elif not lint.data["passed"]:
            reasons.append(f"pack v{v} fails the lint ({lint.data['blocks']} blocking findings)")
        elif lint.data.get("open_questions", 0) and not _last(evs, "questions.sent"):
            reasons.append("the open questions have not been sent to the client, so the client cannot have confirmed the pack")
        opened = _gate(evs, 1, "gate.opened")
        if not opened:
            reasons.append("Gate 1 was never opened (the reviewer was not shown the pack)")
    elif gate == 2:
        if not _last(evs, "tickets.created"):
            reasons.append("tickets have not been created in Pulse")
        if "tech_lead" in required_roles(evs, 2) and not _last(evs, "tech.sized"):
            reasons.append("the tech lead has not sized the stories")
        ready = _last(evs, "tickets.linted", lambda e: e.data.get("stage") == "ready")
        if not ready:
            reasons.append("the ready-stage Definition of Ready check has not run")
        elif not ready.data["passed"]:
            reasons.append(f"Definition of Ready fails ({ready.data['blocks']} blocking findings)")
        drift = _last(evs, "drift.checked")
        if not drift:
            reasons.append("the drift check has not run")
        elif not drift.data["passed"]:
            reasons.append("tickets have drifted from the confirmed pack")
        if not _gate(evs, 2, "gate.opened"):
            reasons.append("Gate 2 was never opened")
    elif gate == 3:
        merged_pr = _last(evs, "pr.opened")
        if not merged_pr:
            return ["no pull request exists"]
        pid = merged_pr.data["pr"]
        shas = [e.data["head_sha"] for e in evs if e.type in ("pr.opened", "pr.pushed") and e.data.get("pr") == pid]
        head = shas[-1]
        v = _last(evs, "parakh.verdict", lambda e: e.data["pr"] == pid and e.data["head_sha"] == head)
        if not v:
            reasons.append(f"no Parakh verdict for the latest commit {head[:7]}")
        elif str(v.data["gate"]).lower() != "pass":
            reasons.append("Parakh gate is not a pass")
    else:
        reasons.append(f"unknown gate {gate}")
    return reasons


def epoch_events(evs: List[Event]) -> List[Event]:
    """After a change request or follow-up call reopens the pack, gates and ticket checks must be redone against the
    new version. These are the events that still count: from the first pack drafted after the latest reopening.
    (Nothing counts until the revised pack exists.)"""
    last = max((i for i, e in enumerate(evs) if e.type == "pack.reopened"), default=-1)
    if last < 0:
        return evs
    for i in range(last + 1, len(evs)):
        if evs[i].type == "pack.drafted":
            return evs[i:]
    return []


def required_roles(evs: List[Event], gate: int) -> List[str]:
    """Roles whose approval completes a gate. Recorded on gate.opened so history keeps the rule it ran under."""
    opened = _gate(epoch_events(evs), gate, "gate.opened")
    return list((opened.data.get("required_roles") if opened else None) or ["delivery_manager"])


def gate_complete(evs: List[Event], gate: int) -> bool:
    evs = epoch_events(evs)
    roles = {e.data["approver_role"] for e in evs
             if e.type == "gate.approved" and str(e.data.get("gate")) == str(gate)}
    return bool(roles) and all(r in roles for r in required_roles(evs, gate))


def _nudge_due(evs: List[Event], check: str, since: datetime, hours: int, now: datetime) -> bool:
    """True when someone has waited longer than the SLA and has not been nudged in the last SLA window."""
    if now - since < timedelta(hours=hours):
        return False
    last = _last(evs, "nudge.sent", lambda e: e.data.get("check") == check)
    return last is None or (now - last.ts) >= timedelta(hours=hours)


def _escalate_target(evs: List[Event], check: str, cfg: dict) -> str:
    n = len(_all(evs, "nudge.sent", lambda e: e.data.get("check") == check))
    limit = cfg["schedules"]["escalate_after_nudges"]
    return "delegate" if n >= limit and cfg.get("people", {}).get("delegate") else "owner"


def _human_wait(evs, check, owner, since, detail, cfg, now, waiting: List[Waiting], orders: List[Order], flow: str) -> None:
    waiting.append(Waiting(flow, check, owner, since, detail))
    hours = cfg["schedules"]["gate_nudge_hours"]
    if since and _nudge_due(evs, check, since, hours, now):
        orders.append(Order(f"{flow}:nudge:{check}", flow, "nudge", f"waiting on {owner} for {check}",
                            inputs={"check": check, "owner": owner, "to": _escalate_target(evs, check, cfg),
                                    "detail": detail}))


def _after_gate2(evs, flow, cfg, now, orders, waiting, support: bool) -> None:
    tier = _last(evs, "tier.set")
    if not tier:
        text = " ".join(str(e.data.get("text") or e.data.get("title") or "") for e in evs
                        if e.type in ("intake.received", "call.recorded"))
        _human_wait(evs, "tier", "tech_lead", evs[-1].ts, "Set the risk tier T1/T2/T3 before work starts.",
                    cfg, now, waiting, orders, flow)
        waiting[-1].detail += f" Suggested: {suggest_tier(text)}."
        return
    if _last(evs, "code.not_needed"):
        return
    branch = _last(evs, "branch.shared")
    if not branch:
        _human_wait(evs, "branch", "tech_lead", tier.ts,
                    "Share the git branch (or say 'no code needed'). The build starts from it.",
                    cfg, now, waiting, orders, flow)
        return
    if not _last(evs, "build.started"):
        orders.append(Order(f"{flow}:launch_build", flow, "launch_build",
                            "branch shared and gates passed; build from the documented requirement",
                            inputs={"repo": branch.data["repo"], "branch": branch.data["branch"],
                                    "tier": tier.data["tier"], "track": "support" if support else "build",
                                    "unsized": (not support) and not _last(evs, "tech.sized")}))
        return
    start = _last(evs, "build.started")
    grace = timedelta(days=3)
    if not _last(evs, "assumptions.received") and now - start.ts > grace:
        _human_wait(evs, "assumptions_note", "developer", start.ts,
                    "Claude Code's assumptions note has not come back to the pack/ticket.", cfg, now, waiting, orders, flow)


def _build_flow(evs, flow, cfg, now, orders, waiting) -> None:
    call = _last(evs, "call.recorded")
    packs = _all(evs, "pack.drafted")
    last_reopen = max((i for i, e in enumerate(evs) if e.type == "pack.reopened"), default=-1)
    last_draft = max((i for i, e in enumerate(evs) if e.type == "pack.drafted"), default=-1)

    # A change request or a follow-up call reopened the pack: revise it before anything else moves.
    if packs and last_reopen > last_draft:
        v = max(p.data["version"] for p in packs)
        why = evs[last_reopen].data["reason"]
        orders.append(Order(f"{flow}:revise_pack:{v}", flow, "revise_pack",
                            f"requirement reopened ({why}); revise the pack to v{v + 1}",
                            inputs={"version": v + 1, "reason": why, "prompt": "prompts/stage2b_revise_pack.md"}))
        return
    # Gates, ticket checks and ticket creation only count if they happened against the current version.
    ev = epoch_events(evs)

    if not packs:
        # Before spending tokens on a draft, check the call covers what a requirement needs. A call that does not
        # is a follow-up call, not a requirement; the delivery manager can still override with a reason.
        assessed = _last(evs, "call.assessed")
        as_idx = max((i for i, e in enumerate(evs) if e.type == "call.assessed"), default=-1)
        fu_idx = max((i for i, e in enumerate(evs) if e.type == "call.followup"), default=-1)
        if not assessed or fu_idx > as_idx:
            orders.append(Order(f"{flow}:assess_call", flow, "assess_call",
                                "check the call covers the six must-haves before drafting (no tokens)"))
            return
        if not assessed.data["ready"] and not _last(evs, "call.override"):
            missing = assessed.data["missing"]
            if not _last(evs, "nudge.sent", lambda e: e.data.get("check") == "followup"):
                orders.append(Order(f"{flow}:request_followup", flow, "request_followup",
                                    f"the call covers {len(assessed.data['covered'])} of 6 must-haves; ask for a follow-up call",
                                    inputs={"missing": missing, "needed": assessed.data.get("needed", 5)}))
            _human_wait(evs, "followup_call", "delivery_manager", assessed.ts,
                        (("The source text is not usable: " + assessed.data["problem"])
                         if assessed.data.get("problem") else
                         "Not enough was covered to draft a requirement. Missing: " + ", ".join(missing)
                         + ". Hold a follow-up call, or proceed anyway with a reason."), cfg, now, waiting, orders, flow)
            return
        orders.append(Order(f"{flow}:draft_pack", flow, "draft_pack", "call recorded and covers what a requirement needs",
                            inputs={"source_ref": call.data.get("source_ref"), "prompt": "prompts/stage2_requirement_pack.md"}))
        return
    v = max(p.data["version"] for p in packs)
    lint = _last(ev, "pack.linted", lambda e: e.data["version"] == v)
    if not lint:
        orders.append(Order(f"{flow}:lint_pack", flow, "lint_pack", f"pack v{v} not linted", inputs={"version": v}))
        return
    if not lint.data["passed"]:
        fails = len(_all(ev, "pack.linted", lambda e: not e.data["passed"]))
        if fails >= MAX_PACK_ATTEMPTS:
            if not _last(ev, "escalated", lambda e: e.data["reason"] == "pack_lint"):
                orders.append(Order(f"{flow}:escalate:pack_lint", flow, "escalate",
                                    f"pack failed lint {fails} times; a person must look",
                                    inputs={"reason": "pack_lint", "to": "delivery_manager"}))
            waiting.append(Waiting(flow, "pack_rework", "delivery_manager", lint.ts,
                                   "The AI could not produce a passing pack from this transcript. Likely a thin call: consider a follow-up call."))
        else:
            orders.append(Order(f"{flow}:fix_pack:{v}", flow, "fix_pack",
                                f"pack v{v} has {lint.data['blocks']} blocking findings (attempt {fails} of {MAX_PACK_ATTEMPTS})",
                                inputs={"version": v + 1, "attempt": fails}))
        return

    if lint.data.get("open_questions", 0) and not _last(ev, "questions.sent"):
        orders.append(Order(f"{flow}:prepare_questions", flow, "prepare_questions",
                            "pack has open questions; draft the numbered email for the delivery manager to send",
                            inputs={"count": lint.data.get("open_questions")}))
        _human_wait(ev, "send_questions", "delivery_manager", lint.ts,
                    "Send the numbered open questions to the client and confirm in Slack.", cfg, now, waiting, orders, flow)

    g1o = _gate(ev, 1, "gate.opened")
    if not g1o or g1o.data.get("pack_version", v) != v:
        orders.append(Order(f"{flow}:open_gate1", flow, "open_gate",
                            f"pack v{v} passed the lint; show it to the reviewer", inputs={"gate": 1, "pack_version": v}))
        return
    g1a = _gate(ev, 1, "gate.approved")
    if not g1a or g1a.data.get("pack_version", v) != v:
        _human_wait(ev, "gate1", "delivery_manager", g1o.ts, "Read the pack and confirm with the client.", cfg, now, waiting, orders, flow)
        return

    revising = bool(_last(evs, "tickets.created"))
    if not _last(ev, "tickets.drafted"):
        act = "revise_tickets" if revising else "draft_tickets"
        orders.append(Order(f"{flow}:{act}", flow, act,
                            f"Gate 1 approved for pack v{v}; " + ("revise the ticket set to match it" if revising else "draft the ticket set"),
                            inputs={"prompt": "prompts/stage3b_revise_tickets.md" if revising else "prompts/stage3_ticket_set.md",
                                    "pack_version": v}))
        return
    draft = _last(ev, "tickets.linted", lambda e: e.data.get("stage") == "draft")
    drafted = _last(ev, "tickets.drafted")
    if not draft or draft.ts < drafted.ts:
        orders.append(Order(f"{flow}:lint_tickets", flow, "lint_tickets", "ticket set drafted, not linted",
                            inputs={"stage": "draft"}))
        return
    drift = _last(ev, "drift.checked")
    if not drift or drift.ts < drafted.ts:
        orders.append(Order(f"{flow}:check_drift", flow, "check_drift", "compare tickets with the confirmed pack"))
        return
    if not draft.data["passed"] or not drift.data["passed"]:
        fails = len(_all(ev, "tickets.linted", lambda e: not e.data["passed"]))
        if fails >= MAX_PACK_ATTEMPTS:
            waiting.append(Waiting(flow, "ticket_rework", "tech_lead", draft.ts, "Tickets failed the check repeatedly."))
        else:
            orders.append(Order(f"{flow}:fix_tickets:{fails}", flow, "fix_tickets",
                                "ticket set fails the Definition of Ready or has drifted", inputs={"attempt": fails}))
        return
    if not _last(ev, "tickets.created"):
        orders.append(Order(f"{flow}:create_pulse_tickets", flow, "create_pulse_tickets",
                            ("the ticket set passes; apply the changes in Pulse (new tickets, change and removal notes)"
                             if revising else "ticket set passes; create it in Pulse in the agreed structure (resumable)")))
        return

    g2o = _gate(ev, 2, "gate.opened")
    created = _last(ev, "tickets.created")
    if (not _last(evs, "tech.sized") and not _last(evs, "build.started")
            and not cfg.get("validation", {}).get("require_tech_lead") and "tech_lead" not in required_roles(ev, 2)):
        waiting.append(Waiting(flow, "sizes", "tech_lead", created.ts,
                               "Optional: size the stories (reply 'S1=5d S2=3d') any time before development starts. "
                               "Not part of validation.", optional=True))
    if not g2o:
        orders.append(Order(f"{flow}:open_gate2", flow, "open_gate",
                            "tickets exist in Pulse; ask the delivery manager to validate before development",
                            inputs={"gate": 2, "required_roles": ["delivery_manager"]
                                    + (["tech_lead"] if cfg.get("validation", {}).get("require_tech_lead") else [])}))
        return
    if not gate_complete(ev, 2):
        req = required_roles(ev, 2)
        sized = _last(evs, "tech.sized")
        ready = _last(ev, "tickets.linted", lambda e: e.data.get("stage") == "ready")
        need_sizes = "tech_lead" in req
        if (sized or not need_sizes) and (not ready or (sized and ready.ts < sized.ts)):
            orders.append(Order(f"{flow}:lint_tickets_ready", flow, "lint_tickets",
                                "run the ready-stage Definition of Ready before validation",
                                inputs={"stage": "ready", "size_optional": not need_sizes}))
        done = {e.data["approver_role"] for e in _all(ev, "gate.approved", lambda e: str(e.data.get("gate")) == "2")}
        for role in req:
            if role not in done:
                detail = ("Validate the tickets before development." if role == "delivery_manager"
                          else "Size each story (reply 'S1=5d S2=3d') and validate.")
                _human_wait(ev, f"gate2_{role}", role, g2o.ts, detail, cfg, now, waiting, orders, flow)
        return
    # The requirement changed after the build began: tell whoever is building, once.
    started = max((i for i, e in enumerate(evs) if e.type == "build.started"), default=-1)
    if last_reopen >= 0 and 0 <= started < last_reopen:
        told = any(e.type == "nudge.sent" and e.data.get("check") == "cr_notify" and i > last_reopen for i, e in enumerate(evs))
        if not told:
            orders.append(Order(f"{flow}:notify_change:{v}", flow, "notify_change",
                                f"the requirement changed after the build started (pack v{v}); tell the developer",
                                inputs={"pack_version": v, "reason": evs[last_reopen].data["reason"]}))
    _after_gate2(evs, flow, cfg, now, orders, waiting, support=False)


def _support_flow(evs, flow, cfg, now, orders, waiting) -> None:
    intake = _last(evs, "intake.received")
    if not _last(evs, "ticket.created"):
        orders.append(Order(f"{flow}:create_support_ticket", flow, "create_support_ticket",
                            "support request received; create the Pulse ticket in the agreed structure",
                            inputs={"source_ref": intake.data["source_ref"],
                                    "suggested_tier": suggest_tier(str(intake.data.get("text", ""))),
                                    "parent": cfg["pulse"].get("support_parent", ""),
                                    "title_prefix": cfg["pulse"].get("title_prefix", "")}))
        return
    _after_gate2(evs, flow, cfg, now, orders, waiting, support=True)


def next_orders(store: EventStore, cfg: dict, now: Optional[datetime] = None,
                rules: Optional[dict] = None) -> Tuple[List[Order], List[Waiting]]:
    now = now or now_utc()
    key = cfg["key"]
    orders: List[Order] = []
    waiting: List[Waiting] = []
    track = cfg["track"]
    sched = cfg["schedules"]
    polls = store.events(flow="_poll")

    for flow in store.flows(key):
        evs = store.events(flow=flow)
        if not evs:
            continue
        if track == "build" and _last(evs, "call.recorded"):
            _build_flow(evs, flow, cfg, now, orders, waiting)
        elif track == "support" and _last(evs, "intake.received"):
            _support_flow(evs, flow, cfg, now, orders, waiting)

    if (track == "build" and cfg.get("granola", {}).get("source", "paste") == "connector"
            and _due(polls, "granola", sched["granola_poll_min"], now)):
        orders.append(Order("_poll:granola", "_poll", "poll_granola", "look for new call notes in the project's folder",
                            kind="poll", inputs={"folder": cfg["granola"]["folder"], "title_tag": cfg["granola"]["title_tag"]}))
    if _due(polls, "slack", sched["slack_poll_min"], now) and (waiting or track == "support"):
        inputs = {"channel": cfg["slack"]["channel"], "waiting_flows": sorted({w.flow for w in waiting})}
        if track == "support":
            inputs["intake_channel"] = cfg["slack"]["intake_channel"]
        orders.append(Order("_poll:slack", "_poll", "poll_slack",
                            "read replies for open gates, sizes, tiers and branches" + ("; read new support requests" if track == "support" else ""),
                            kind="poll", inputs=inputs))
    if _due(polls, "pulse_sweep", sched["pulse_sweep_min"], now):
        orders.append(Order("_poll:pulse_sweep", "_poll", "sweep_pulse", "check board tickets against the Definition of Ready",
                            kind="poll", inputs={"project_key": cfg["pulse"]["project_key"], "mode": cfg["mode"]}))
    if _due(polls, "governance", sched["governance_refresh_min"], now):
        orders.append(Order("_poll:governance", "_poll", "refresh_governance", "recompute scores and the efficiency mapping",
                            kind="poll"))
    for o in orders:
        assign_model(o, store.events(flow=o.flow) if o.flow != "_poll" else [], cfg, rules)
    return orders, waiting
