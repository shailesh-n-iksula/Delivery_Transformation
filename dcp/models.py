"""Which steps need an AI model, which tier each one runs on, and when a tier escalates.

Three kinds of step:
  code       lint, drift check: plain code, never a model.
  connector  reaches Granola, Slack, Pulse or GitHub, or posts a templated message. Mechanical, so the smallest
             tier is enough. While the connectors only exist inside a Claude session (runner mode
             'claude-session') a session is needed to reach them, so 'none' is not allowed. Once direct API
             access exists (runner mode 'api') it can be 'none'.
  ai         drafting text: the pack, the tickets, a support ticket, the client's questions.

A tier is a size, not a model name. config/rules.json maps tiers to model aliases, so changing the model behind
'standard' is one edit. Because every draft is checked in code, cheap-first is safe: a weak draft cannot pass,
it only costs a retry, and after `escalate_after_failures` failed checks the step moves up one tier.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

ACTION_KIND: Dict[str, str] = {
    "lint_pack": "code", "lint_tickets": "code", "check_drift": "code", "assess_call": "code",
    "poll_granola": "connector", "poll_slack": "connector", "sweep_pulse": "connector",
    "refresh_governance": "connector", "open_gate": "connector", "nudge": "connector",
    "escalate": "connector", "create_pulse_tickets": "connector", "launch_build": "connector",
    "request_followup": "connector", "notify_change": "connector",
    "revise_pack": "ai", "revise_tickets": "ai",
    "draft_pack": "ai", "fix_pack": "ai", "draft_tickets": "ai", "fix_tickets": "ai",
    "create_support_ticket": "ai", "prepare_questions": "ai",
}
STEP_LABEL: Dict[str, str] = {
    "draft_pack": "Draft the requirement pack", "fix_pack": "Redraft the pack after a failed check",
    "draft_tickets": "Draft the Pulse ticket set", "fix_tickets": "Redraft tickets after a failed check",
    "create_support_ticket": "Draft a support ticket from a Slack thread", "prepare_questions": "Word the questions for the client",
    "poll_granola": "Look for new call notes", "poll_slack": "Read replies and new requests",
    "sweep_pulse": "Read board tickets for the Definition-of-Ready sweep", "refresh_governance": "Post the governance summary",
    "open_gate": "Post a gate prompt", "nudge": "Post a nudge", "escalate": "Post an escalation",
    "create_pulse_tickets": "Create the tickets in Pulse", "launch_build": "Post the build brief and command",
    "revise_pack": "Revise the pack after a change request or follow-up call", "revise_tickets": "Revise the ticket set to match a revised pack",
    "notify_change": "Tell the developer the requirement changed",
    "assess_call": "Check the call covers what a requirement needs", "request_followup": "Ask for a follow-up call",
    "lint_pack": "Check the pack", "lint_tickets": "Check the tickets", "check_drift": "Compare tickets with the pack",
}
TIERS = ("none", "small", "standard", "strong")
LADDER = ("small", "standard", "strong")
DEFAULT_TIER_ALIAS = {"small": "haiku", "standard": "sonnet", "strong": "opus"}
DEFAULT_MODELS: Dict[str, str] = {
    **{a: "none" for a, k in ACTION_KIND.items() if k == "code"},
    **{a: "small" for a, k in ACTION_KIND.items() if k == "connector"},
    "draft_pack": "standard", "fix_pack": "standard", "draft_tickets": "standard", "fix_tickets": "standard",
    "revise_pack": "standard", "revise_tickets": "standard",
    "create_support_ticket": "small", "prepare_questions": "small",
}
DEFAULT_RUNNER = {"mode": "claude-session"}
DEFAULT_ESCALATE_AFTER = 2


def effective_models(cfg: dict) -> Dict[str, str]:
    return {**DEFAULT_MODELS, **(cfg.get("models") or {})}


def tier_alias(tier: str, rules: Optional[dict] = None) -> Optional[str]:
    if tier == "none":
        return None
    table = ((rules or {}).get("models") or {}).get("tiers") or DEFAULT_TIER_ALIAS
    return table.get(tier, DEFAULT_TIER_ALIAS.get(tier))


def validate_models(cfg: dict) -> List[str]:
    problems: List[str] = []
    models = cfg.get("models") or {}
    mode = (cfg.get("runner") or DEFAULT_RUNNER).get("mode", "claude-session")
    if mode not in ("claude-session", "api"):
        problems.append(f"runner.mode: '{mode}' must be 'claude-session' or 'api'")
    for action, tier in models.items():
        if action not in ACTION_KIND:
            problems.append(f"models.{action}: unknown step. Known: {', '.join(sorted(ACTION_KIND))}")
            continue
        if tier not in TIERS:
            problems.append(f"models.{action}: '{tier}' is not a tier. Use one of {', '.join(TIERS)}")
            continue
        kind = ACTION_KIND[action]
        if kind == "ai" and tier == "none":
            problems.append(f"models.{action}: drafting needs a model. Use small, standard or strong.")
        if kind == "code" and tier != "none":
            problems.append(f"models.{action}: this step is plain code and never uses a model. Leave it as 'none'.")
        if kind == "connector" and tier == "none" and mode != "api":
            problems.append(f"models.{action}: 'none' needs direct API access (runner.mode = 'api'). "
                            "Until then a small model reaches the connector.")
    n = (cfg.get("escalate_after_failures", DEFAULT_ESCALATE_AFTER))
    if not isinstance(n, int) or n < 1:
        problems.append("escalate_after_failures: must be a whole number of at least 1")
    return problems


def failures_for(action: str, evs) -> int:
    """How many times the checks have rejected this step's output so far."""
    if action in ("draft_pack", "fix_pack"):
        return sum(1 for e in evs if e.type == "pack.linted" and not e.data["passed"])
    if action in ("draft_tickets", "fix_tickets"):
        return (sum(1 for e in evs if e.type == "tickets.linted" and not e.data["passed"])
                + sum(1 for e in evs if e.type == "drift.checked" and not e.data["passed"]))
    return 0


def escalate(tier: str, failures: int, limit: int) -> Tuple[str, bool]:
    if tier not in LADDER or failures < limit:
        return tier, False
    idx = min(LADDER.index(tier) + failures // limit, len(LADDER) - 1)
    return LADDER[idx], LADDER[idx] != tier


def assign(order, evs, cfg: dict, rules: Optional[dict] = None) -> None:
    """Sets order.tier / order.model / order.needs_ai in place."""
    kind = ACTION_KIND.get(order.action, "connector")
    tier = effective_models(cfg).get(order.action, "small" if kind != "code" else "none")
    if kind == "code":
        tier = "none"
    escalated = False
    if kind == "ai":
        tier, escalated = escalate(tier, failures_for(order.action, evs), cfg.get("escalate_after_failures", DEFAULT_ESCALATE_AFTER))
    mode = (cfg.get("runner") or DEFAULT_RUNNER).get("mode", "claude-session")
    if kind == "connector" and tier == "none" and mode != "api":
        tier = "small"
    order.step_kind = kind
    order.tier = tier
    order.model = tier_alias(tier, rules)
    order.needs_ai = kind == "ai"
    if escalated:
        order.inputs = {**order.inputs, "escalated_to": tier}
