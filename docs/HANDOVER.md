# Handover: Delivery Control Plane

**Start here.** This is for the tech team taking this from a working, tested core to a production tool.
Read this page, then `ARCHITECTURE.md`, `ADAPTERS.md` and `SECURITY.md`. Open questions are in `OPEN_QUESTIONS.md`;
the build backlog is in `BACKLOG.md`.

## What it is

A system that turns the Technical Delivery AI Playbook from advice into controls. A client call becomes a requirement
pack, then Pulse tickets, then a build brief for a branch, then a reviewed pull request, and every step is checked in
code and recorded in a tamper-evident log. People give a few structured inputs and approve at gates; the system does
the rest and measures whether the process was followed and what it saved.

Design rule: **every guarantee is enforced in code and proved by a test.** "Foolproof" here means a lazy or rushed step is
stopped or flagged, not that nothing can go wrong. Where a rule is a heuristic (for example, whether a call covered what
a requirement needs), it is written to fail safe and can be overridden by a named person with a recorded reason.

## Status at a glance

| Area | Status |
|---|---|
| Event log (text file and SQLite), tamper detection, migration between them | **Built, tested** |
| Pack and ticket checks, drift check, call-readiness check, quote traceability | **Built, tested** |
| Paste-mode transcripts (summary refused, duplicates, follow-up calls) | **Built, tested** |
| Flow state machine: orders, waiting, nudges, escalation, revisions after a change request | **Built, tested** |
| Gates with hard stops, roles by Slack ID, delegate, version-bound approvals | **Built, tested** |
| Pulse ticket plan (resumable; change and removal notes; label and epic by convention) | **Built, tested against a plan only** |
| GitHub flow branch rules, build brief | **Built, tested** |
| 21 compliance checks, disputes, break-glass, optional sizing, hours and rework import | **Built, tested** |
| Models per step, token log, no-token idle tick | **Built, tested** |
| Manual-mode worksheet (every step usable with no connection) | **Built, tested** |
| GitHub webhook mapping and signature checks; Slack signature check | **Built, tested with GitHub's payload shapes** |
| Reference server, dashboard, repo readiness check, pull request check | **Built, tested; not run in production** |
| Docker, compose, cron, GitHub Action template | **Written, never run** (no Docker or Actions runner was available) |
| `dcp tick --launch` (headless model sessions) | **Experimental, unverified** (needs a signed-in `claude` and connectors) |
| **Pulse API adapter** | **Placeholder: yours to build** (see below) |
| **Slack app and event mapping** | **Placeholder: yours to build** |
| **GitHub App, required check, branch protection** | **Reference code exists; yours to finalise** |
| **Granola transcript access** | **Paste mode today; API needs a paid Granola tier** |
| Model calls through an API (drafting without a person) | Not built |
| Dashboard login, per-person views, org roll-up | Not built |

320 automated tests run in about 8 seconds: `python3 -m unittest discover -s tests -t .` Python 3.9+, standard library only.

## Notes the tech team needs, by connection

**Pulse (not yet connected; the tech team adds the API so it is seamless).**
The connector available today can create only `story`, `task` and `bug` issues with a plain-text description. It cannot
set labels, an epic type, a parent, issue links, a sprint, an assignee or an estimate, cannot update an issue, and cannot
read worklogs. The system works around this by convention (`EPIC:`, `DEP:`, `RISK:`, `DECISION:` title prefixes, a
`Label:` line, comments for links and sizes) and every plan operation already carries the field it will use once the API
exists. The full request is in `docs/pulse-api-request.md`. Also wanted: a workflow rule that blocks In Progress without
acceptance criteria and an assignee. Until the API exists, `dcp worksheet` prints each ticket to create by hand.

**GitHub (connected to Claude for the owner, but not wired into this system; the tech team finalises it before the
system is declared final).** Reference code is in `dcp/webhooks.py` (event mapping, signatures), `dcp/prcheck.py` (the
required status check), `dcp/repocheck.py` (readiness and branch protection), `templates/` (PR template, Action) and
`dcp/server.py`. To do: register a GitHub App (or per-repo webhooks) pointing at `/webhooks/github`, set the secret,
install the Action, make `delivery-check / pr-check` a **required** status check, turn on branch protection, and have the
Parakh owners emit the footer in `ADAPTERS.md`. Until then PR events can be logged by hand (`dcp log`).

**Slack (not yet built).** Gate approvals today are recorded by command, with the approver identified by **Slack member ID**.
If a person cannot be matched automatically, the system asks for their ID (profile, three dots, Copy member ID) and registers
them with `dcp people`. The webhook endpoint verifies Slack signatures and answers the URL handshake; mapping events to gates
needs the Slack app and a store of gate-message IDs. The spec is in `ADAPTERS.md`. Slack reaction events carry no timestamp, so
gate review time is accurate only to the poll interval (a limitation to design around, not a bug).

**Granola (paste mode).** The owner's plan returns only the AI summary through the connector, never the transcript. The
summary is **never** used as a source: a pasted summary is refused. Transcripts are pasted into `data/<KEY>/inbox/`. Paid tiers
expose transcripts to the connector, which would let `granola.source = "connector"` work.

## Manual fallbacks, per connection

| Where the system cannot get it | What a person does |
|---|---|
| Granola transcript | Paste it into `data/<KEY>/inbox/` (`dcp ingest`) |
| A Slack person or reaction | Give their Slack ID; `dcp people add` once, then `--by-id` on any command |
| Slack messages out | `dcp worksheet` prints the exact text to post |
| Pulse create / read | `dcp worksheet` prints each ticket; `dcp tickets-record` records its key; `dcp lint-pulse --export` reads an export |
| GitHub events | `dcp log pr.opened ...` or `dcp webhook-test --file payload.json` |
| The drafting model | Run the prompt in an approved assistant, save the file, `dcp tick` |

## Assumptions made (change any of them; each is one setting or one test)

1. One GitHub repo per project; GitHub flow only (`feature/`, `bugfix/`, `hotfix/` + ticket key).
2. The delivery manager validates and approves Gate 1; the tech lead is optional for validation, owns sizing (optional), sets the tier and merges.
3. A practice lead (one per project, confirmed) resolves score disputes. No one resolves their own.
4. Pilot storage is the text file; SQLite for a team; Postgres later behind the same interface.
5. Transcripts are kept as long as the project (no automatic deletion yet): needs a retention decision.
6. A call needs 5 of 6 must-haves to be drafted from; the delivery manager can override with a reason.
7. A gate approved in under 60 seconds is flagged as unread; the floor and others are in `config/rules.json`.
8. Model sizes: small = haiku, standard = sonnet, strong = opus; relative prices are **placeholders**.
9. Change requests are opened by the delivery manager, with the client's agreement recorded in the reason.
10. Break-glass is allowed, recorded, and must be reviewed by someone else within 24 hours.
11. Polling floors are 5 minutes (connector rate limits).
12. The dashboard is for managers, roles not names (confirmed). Whether individuals see their own view is still open.
13. It runs in a cloud account Iksula already uses (confirmed). Transcripts and logs are kept for the life of the project (confirmed); deletion on a client's request is not built yet.

## Known limits

- Coverage of the six must-haves is a keyword heuristic tuned for precision. It stops obviously thin calls; it does not understand them.
- Quote checking proves words exist in the text it is given, not that the AI understood them. A person still reads the pack.
- Tickets in Pulse are matched to the ticket set by **order and wording**. The revision prompt tells drafters never to reorder; the plan warns when it sees a reorder.
- `tickets.created` is logged when the creates are done; link comments may still be pending after that.
- Nothing has yet run on a real client requirement call from start to finish. See `TEST_PLAN.md`.

## What "final" means: acceptance checklist

1. Pulse, Slack and GitHub adapters live, each with the manual fallback still working.
2. A pilot of 5+ real client requirement calls end to end, with the results in `TEST_PLAN.md`.
3. Security review passed (`SECURITY.md`), including client data sent to models.
4. Branch protection and the required check on every pilot repo (`dcp repo-check --github` passes).
5. A measured baseline from Pulse history, so "efficiency gained" compares against something real.
6. Model bake-off done; tiers chosen with numbers; real prices in `config/rules.json`.
7. Owner named for the system, for the rules file, and for dispute resolution.
