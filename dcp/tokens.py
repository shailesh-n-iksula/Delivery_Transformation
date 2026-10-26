"""Token accounting from `ai.used` events: what each step costs, which model, and how often a model's draft
passed the checks first time. Used to choose tiers with numbers instead of guesses.

Costs are RELATIVE UNITS from config/rules.json (`models.relative_cost`). Those weights are placeholders: replace
them with real per-token prices before reading anything into the cost figures.
"""
from __future__ import annotations

from typing import Dict, List, Optional

PACK_ACTIONS = ("draft_pack", "fix_pack")
TICKET_ACTIONS = ("draft_tickets", "fix_tickets")


def _tier_of(model: str, rules: dict) -> Optional[str]:
    tiers = ((rules or {}).get("models") or {}).get("tiers") or {}
    for tier, alias in tiers.items():
        if alias == model:
            return tier
    return None


def _weight(model: str, tier: Optional[str], rules: dict) -> Optional[float]:
    weights = ((rules or {}).get("models") or {}).get("relative_cost") or {}
    t = tier or _tier_of(model, rules)
    return weights.get(t) if t else None


def summarize(events, rules: dict) -> dict:
    by_action: Dict[str, dict] = {}
    by_model: Dict[str, dict] = {}
    total = {"calls": 0, "input": 0, "output": 0, "cost_units": 0.0, "unpriced_tokens": 0}
    for e in events:
        if e.type != "ai.used":
            continue
        tin, tout = int(e.data["input_tokens"]), int(e.data["output_tokens"])
        w = _weight(e.data["model"], e.data.get("tier"), rules)
        cost = (tin + tout) * w / 1000.0 if w is not None else None
        for bucket, key in ((by_action, e.data["action"]), (by_model, e.data["model"])):
            b = bucket.setdefault(key, {"calls": 0, "input": 0, "output": 0, "cost_units": 0.0})
            b["calls"] += 1
            b["input"] += tin
            b["output"] += tout
            b["cost_units"] += cost or 0.0
        total["calls"] += 1
        total["input"] += tin
        total["output"] += tout
        if cost is None:
            total["unpriced_tokens"] += tin + tout
        else:
            total["cost_units"] += cost
    for b in list(by_action.values()) + list(by_model.values()):
        b["avg_tokens_per_call"] = round((b["input"] + b["output"]) / b["calls"]) if b["calls"] else 0
        b["cost_units"] = round(b["cost_units"], 2)
    total["cost_units"] = round(total["cost_units"], 2)
    total["tokens"] = total["input"] + total["output"]
    return {"total": total, "by_action": by_action, "by_model": by_model}


def outcomes(events) -> Dict[str, dict]:
    """Per model: how many drafts the checks accepted. The next check result after a draft is that draft's verdict.
    first_try = the first draft of a pack or ticket set in a flow (no redraft yet)."""
    by_model: Dict[str, dict] = {}
    flows: Dict[str, list] = {}
    for e in sorted(events, key=lambda x: x.ts):
        flows.setdefault(e.flow, []).append(e)
    for evs in flows.values():
        first_seen = set()
        for i, e in enumerate(evs):
            if e.type != "ai.used" or e.data["action"] not in PACK_ACTIONS + TICKET_ACTIONS:
                continue
            kind = "pack" if e.data["action"] in PACK_ACTIONS else "tickets"
            verdict = None
            for later in evs[i + 1:]:
                if kind == "pack" and later.type == "pack.linted":
                    verdict = bool(later.data["passed"])
                    break
                if kind == "tickets" and later.type == "tickets.linted" and later.data.get("stage", "draft") == "draft":
                    verdict = bool(later.data["passed"])
                    break
            if verdict is None:
                continue
            m = by_model.setdefault(e.data["model"], {"drafts": 0, "passed": 0, "first_try_drafts": 0, "first_try_passed": 0})
            m["drafts"] += 1
            m["passed"] += int(verdict)
            if kind not in first_seen:
                first_seen.add(kind)
                m["first_try_drafts"] += 1
                m["first_try_passed"] += int(verdict)
    for m in by_model.values():
        m["pass_rate"] = round(m["passed"] / m["drafts"] * 100) if m["drafts"] else None
        m["first_try_pass_rate"] = round(m["first_try_passed"] / m["first_try_drafts"] * 100) if m["first_try_drafts"] else None
    return by_model


def per_flow(events, rules: dict) -> Dict[str, dict]:
    flows: Dict[str, list] = {}
    for e in events:
        if e.type == "ai.used":
            flows.setdefault(e.flow, []).append(e)
    return {f: summarize(evs, rules)["total"] for f, evs in flows.items()}
