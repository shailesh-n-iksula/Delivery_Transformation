"""Generates SYNTHETIC demo events so the scoring and mapping can be seen end to end.

This is invented data. The relationship between compliance and rework in it is built in on
purpose, to show the mechanism. It says nothing about real teams. Real events come from the
adapters (Pulse, GitHub, Slack, Granola).

    python3 examples/make_sample_events.py examples/events_sample.jsonl
"""
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dcp.events import EventStore  # noqa: E402

PROJECT = "SP"


def build_flow(store: EventStore, n: int, quality: float, rng: random.Random) -> None:
    """quality 0..1: higher means more of the playbook was followed."""
    flow = f"SP-REQ-{n:03d}"
    t0 = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc) + timedelta(days=n * 3)
    good = lambda threshold: quality + rng.uniform(-0.12, 0.12) >= threshold  # noqa: E731

    def ev(type_, dt, actor, data, track="build"):
        store.append(type_, flow, PROJECT, actor, data, ts=dt, track=track)

    title = f"ACME Magento :: returns export {n} :: SP" if good(0.2) else f"returns call {n}"
    ev("call.recorded", t0, "dm1", {"title": title})
    pack_at = t0 + timedelta(minutes=12 if good(0.35) else 240)
    ev("pack.drafted", pack_at, "dm1", {"version": 1})
    lint_ok = good(0.45)
    ev("pack.linted", pack_at + timedelta(minutes=3), "dcp",
       {"version": 1, "passed": lint_ok, "blocks": 0 if lint_ok else 5, "warns": 1, "open_questions": 3})
    ev("questions.sent", t0 + (timedelta(hours=3) if good(0.5) else timedelta(days=3)), "dm1", {"count": 3})
    g1_open = pack_at + timedelta(minutes=10)
    ev("gate.opened", g1_open, "dcp", {"gate": 1})
    ev("gate.approved", g1_open + (timedelta(minutes=6) if good(0.4) else timedelta(seconds=15)),
       "dm1", {"gate": 1, "approver": "dm1", "approver_role": "delivery_manager"})
    tix_at = g1_open + timedelta(minutes=40)
    ev("tickets.created", tix_at, "dm1", {"count": 7})
    ready = good(0.55)
    ev("tickets.linted", tix_at + timedelta(minutes=5), "dcp",
       {"passed": ready, "blocks": 0 if ready else 4, "warns": 2, "stage": "ready",
        "owner_date_gaps": 0 if good(0.5) else 3})
    drift_ok = good(0.6)
    ev("drift.checked", tix_at + timedelta(minutes=6), "dcp", {"passed": drift_ok, "blocks": 0 if drift_ok else 2})
    ev("gate.opened", tix_at + timedelta(minutes=10), "dcp", {"gate": 2})
    role2 = "delivery_manager" if good(0.3) else "developer"
    ev("gate.approved", tix_at + timedelta(hours=2), "tl1", {"gate": 2, "approver": "tl1", "approver_role": role2})
    tier_first = good(0.5)
    build_at = t0 + (timedelta(hours=6) if good(0.55) else timedelta(days=4))
    ev("tier.set", build_at - timedelta(hours=1 if tier_first else -1), "tl1", {"tier": "T2", "actor_role": "tech_lead"})
    ev("build.started", build_at, "dev1", {"ticket": f"SP-{n}0"})
    if good(0.6):
        ev("assumptions.received", build_at + timedelta(hours=5), "dev1", {"ticket": f"SP-{n}0"})
    pr_at = build_at + timedelta(days=2)
    ev("pr.opened", pr_at, "dev1",
       {"pr": n, "author": "dev1", "head_sha": f"sha{n}a", "ticket": f"SP-{n}0" if good(0.45) else "",
        "ai_declared": good(0.4), "tier": "T2"})
    if good(0.5):
        ev("parakh.verdict", pr_at + timedelta(minutes=30), "dev1",
           {"pr": n, "head_sha": f"sha{n}a", "gate": "pass", "critical": 0, "major": 0})
    independent = good(0.35)
    ev("pr.merged", pr_at + timedelta(days=1), "tl1",
       {"pr": n, "merger": "tl1", "merger_role": "tech_lead" if good(0.3) else "developer",
        "approvers": ["tl1"] if independent else ["dev1"]})
    ev("release.demoed", pr_at + timedelta(days=2), "dm1", {"against_criteria": good(0.5)})

    est = 80
    actual = round(est * (1.0 + (1 - quality) * 0.55 + rng.uniform(-0.05, 0.05)))
    ev("hours.recorded", pr_at + timedelta(days=3), "dcp", {"estimate_hours": est, "actual_hours": actual})
    for _ in range(round((1 - quality) * 5 + rng.uniform(-0.6, 0.6))):
        kind = rng.choice(["ticket.reopened", "change_request.raised", "defect.found", "assumption_gap.found"])
        data = {"ticket.reopened": {"ticket": f"SP-{n}0"}, "change_request.raised": {"reason": "client changed mind"},
                "defect.found": {"phase": "uat"}, "assumption_gap.found": {"phase": "uat"}}[kind]
        ev(kind, pr_at + timedelta(days=4), "dm1", data)


def main(path: str) -> None:
    p = Path(path)
    if p.exists():
        p.unlink()
    store = EventStore(p)
    rng = random.Random(42)
    qualities = [0.95, 0.9, 0.85, 0.8, 0.75, 0.6, 0.5, 0.45, 0.3, 0.2, 0.9, 0.35]
    for i, q in enumerate(qualities, start=1):
        build_flow(store, i, q, rng)
    print(f"wrote {p} ({len(qualities)} synthetic flows)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "examples/events_sample.jsonl")
