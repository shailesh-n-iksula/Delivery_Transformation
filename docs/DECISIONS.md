# Decisions

Decided by the owner (Shailesh) unless marked "assumed". Each line says where it lives, so a change is easy to find.

| # | Decision | Why | Where |
|---|---|---|---|
| 1 | The flow is: call, Granola, questions and RAID, requirement pack, system asks clarifying questions, then Pulse (epic, story or task, risk or dependency), validation, then Git | The owner's description of the workflow | `flowstate.py`, prototype |
| 2 | Risks and dependencies are `task` issues with a label | The owner's choice; the label needs the Pulse API | `pulseplan.py` (`labels`) |
| 3 | The title keeps the `RISK:` / `DEP:` prefix as well as the label | One naming convention, easy to read | `pulseplan.PREFIX` |
| 4 | Every Pulse ticket carries all its information (client's words, criteria, context, answers) | "Tickets created on Pulse should have all the information" | `ticketlint` TK016 |
| 5 | Branching is GitHub flow only: `feature/`, `bugfix/`, `hotfix/` + ticket key, into the base branch | Owner's choice | `project.branch_problem` |
| 6 | A change is a bug fix **or** a hotfix, never both | Owner's correction | prototype; `branch-name --type` |
| 7 | The delivery manager must validate before development; the tech lead is optional | Owner's choice | `flowstate.gate_complete`, `validation.require_tech_lead` |
| 8 | Sizing is a separate optional step for the tech lead, any time before development | Owner's choice | `cli_ops.cmd_tickets_size`, check C20 (weight 0) |
| 9 | Transcripts are pasted; the AI summary is never a source | Granola's plan exposes no transcript; summaries are paraphrases | `ingest.py` |
| 10 | If a Slack person is not found, the input is their Slack member ID | Owner's choice | `cli_extra.cmd_people`, `require_person` |
| 11 | The tech team adds the Pulse API; the tech lead requests it | Owner's choice | `docs/pulse-api-request.md` |
| 12 | GitHub is connected for the owner; the tech team finalises it before the system is called final | Owner's note | `docs/HANDOVER.md` |
| 13 | Each step can run on a model size chosen per step; checks never use a model; escalate after repeated failed checks | Keep token use low | `models.py` |
| 14 | Build standalone first; fit Brain and Spine later | Owner's choice | adapters are the only coupling |
| 15 | Scores serve both coaching and the performance conversation, with the evidence beside each number and a dispute path | Owner's choice | `dashboard.py`, disputes |
| 16 | Roll out at organisation level with minimal change per project | Owner's choice | project files, `rules.json` |
| 17 | No separate client sign-off step: clarity is the gate | Owner's workflow goes straight to Pulse when clear | `can_approve(1)` |
| 18 | Hard stops only for objective checks; subjective ones nudge or observe | Hard stops on judgement get worked around | design rule |
| 19 | Standard library only, Python 3.9+ | No supply chain, runs anywhere | everywhere |
| 20 | The tier a lead sets can never be lowered by a PR's own declaration | A maker must not choose their own bar | `scoring.Ctx.tier` |
| 21 | A call needs 5 of 6 must-haves to be drafted from (assumed) | Spend nothing drafting from a thin call; override allowed | `rules.json` `call_ready_min_topics` |
| 22 | Disputes are resolved by a practice lead, one per project (confirmed 2026-10-25) | Nobody judges their own score | `cli_extra.cmd_dispute_resolve` |
| 23 | It runs in a cloud account Iksula already uses (confirmed 2026-10-25) | Fits the Docker and compose starting points | `deploy/` |
| 24 | Transcripts and logs are kept for the life of the project (confirmed 2026-10-25); deletion on client request still to be built | Simplest policy that meets the need | `docs/SECURITY.md` item 13 |
| 25 | The dashboard is for managers, roles not names (confirmed 2026-10-25) | Scores are sensitive; per-person views are a separate decision | `dashboard.py` |
