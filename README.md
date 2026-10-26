# Delivery Control Plane (v0.3)

Turns the Technical Delivery AI Playbook from advice into checks. People do a few structured things (paste the call, approve
three gates, answer flagged questions). Everything else is computed, enforced and recorded. Pure Python 3.9+, standard library only,
nothing here holds credentials.

**Handing this to the tech team? Start with [docs/HANDOVER.md](docs/HANDOVER.md).** It says what is built and tested, what is a placeholder
for them, and what "final" means.

| Read | For |
|---|---|
| [docs/HANDOVER.md](docs/HANDOVER.md) | Status, notes on Pulse / GitHub / Slack / Granola, manual fallbacks, assumptions, acceptance checklist |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it fits together, the modules, extension points |
| [docs/ADAPTERS.md](docs/ADAPTERS.md) | What each adapter must do, field by field |
| [docs/SECURITY.md](docs/SECURITY.md) | Threats, controls, what is still open |
| [docs/TEST_PLAN.md](docs/TEST_PLAN.md) | What is proved, what is not, and how to prove it |
| [docs/BACKLOG.md](docs/BACKLOG.md), [docs/backlog_tickets.md](docs/backlog_tickets.md) | The build backlog, in this system's own ticket format |
| [docs/DECISIONS.md](docs/DECISIONS.md), [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) | Decisions made, and questions with the default assumed |
| [deploy/](deploy/README.md) | Docker, compose, cron, `.env.example` (never run) |

```bash
python3 -m unittest discover -s tests -t .      # about 320 tests, under 10 seconds, no network
python3 -m dcp --help                           # 46 commands
python3 -m dcp worksheet --project SP           # what to do now, by hand, if nothing is connected
```

The Granola, Slack and Pulse connectors exist inside Claude sessions today, so a scheduled Claude task can do the fetching and posting
(`runner/RUNNER.md`) while this code decides what is due and checks every result. The tech team replaces that with real adapters.

## What is here

| Piece | File | What it does |
|---|---|---|
| Rules | `config/rules.json` | Every threshold, weight, vague-word list and role. Edit this, not code. |
| Event log | `dcp/events.py` | Append-only, hash-chained. Every score is computed from it. Tampering is detected. |
| Pack linter | `dcp/packlint.py` | Gate 1 helper. Every scope item and criterion needs a `[Q: "..."]` quote that exists in the transcript. Blocks invented scope, vague criteria, missing owners and dates, commitment language. |
| Ticket linter | `dcp/ticketlint.py` | Story template and Definition of Ready as code. Two stages: `draft` (what AI wrote) and `ready` (Gate 2). |
| Drift check | `dcp/drift.py` | Pack vs tickets. Catches scope added or dropped after the client confirmed, comparing Given, When and Then separately. |
| Scoring | `dcp/scoring.py` | 21 compliance checks across the build and support tracks, speed metrics, rework counts, disputes, and the compliance-to-outcome mapping. |
| Prompts | `prompts/` | Stage 2 and 3 prompts rewritten to emit exactly what the linters read. |


## Per-project setup and what runs when

A project is registered once with the few inputs it needs. A support project that already has a Slack
channel and a Pulse board needs even less (no Granola, no repo until a fix needs one).

```bash
# build project: every input
python3 -m dcp project init --track build \
  --pulse-url https://pulse.iksula.com/projects/SP/board --slack-channel '#sp-delivery' \
  --granola-folder 'SiteOne Pimcore' --git-repo iksula/siteone-pimcore \
  --delivery-manager 'Meera N:U01ABC2345' --tech-lead 'Arjun K:U01DEF6789'

# support project that already exists: board URL (key is read from it) + channel + tech lead
python3 -m dcp project init --track support --pulse-url https://pulse.iksula.com/projects/ML/board \
  --slack-channel '#ml-support' --tech-lead 'Arjun K:U01DEF6789' --support-parent ML-1454

python3 -m dcp project validate SP      # says exactly what is still missing
python3 -m dcp next --project SP        # what is due now, and who is being waited on
```

| Input | Where it comes from | Who gives it |
|---|---|---|
| Pulse board, Slack channel, repo, people (and a Granola folder only in connector mode) | `projects/<KEY>.json` | once per project |
| The call | a Granola note titled `Project :: topic :: KEY` | delivery manager, while naming the note |
| A support request | a message in the intake channel | whoever reports it |
| The git branch | a reply or `dcp branch-share` | tech lead, once Gate 2 has passed |
| Gates, tier, sizes | a tick or a short reply in Slack | the named person |

Everything else (flow ids, the pack, the tickets, links, nudges, the brief) is derived.

**Intervals** are per project under `schedules`: Granola every 15 min, Slack replies and support intake
every 5 min, Pulse Definition-of-Ready sweep and governance refresh daily, nudge after 4 hours of waiting,
escalate to the delegate after 2 nudges. Floors apply because of connector rate limits.

**Hard stops inside the orchestrator** (not advice): Gate 1 cannot be approved unless the pack passed the
lint, was shown to the reviewer and its open questions went to the client. **Gate 2 is validated by the
delivery manager** and needs a passing ready-stage check, no drift and closed decisions. **The tech lead is
optional**: set `validation.require_tech_lead` to true in the project file and the tech lead must validate too,
with sizes. The roles required are recorded on the gate when it opens, so history keeps the rule it ran
under. Approvers are identified by Slack id and role from the project file, not by a name typed in a message.
A delegate counts only for the role they cover. The build brief refuses to exist before both gates, from
tickets that drifted, or on a branch that breaks the branching strategy.

**Sizing is a separate, optional step for the tech lead.** It is not part of validation. The tech lead (or
the delegate covering them) can size the stories any time after the tickets exist and until development
starts, with `dcp tickets-size --by-id <Slack id>`; anyone else is refused, and it closes once the build
starts. `dcp next` lists it as an optional wait that never blocks and is never nudged. If the build starts
unsized, the launch order says so and the brief warns that effort is unestimated. The sizing check (C20) is
shown in the scorecard with weight 0: visible, never penalised. Sizes become a Pulse comment
(`Size (tech lead): 5 days`) and are the basis for estimate vs actual hours.

**Branching strategy: GitHub flow.** Branches are `feature/`, `bugfix/` or `hotfix/` followed by a Pulse
ticket key and a short description (`feature/SP-123-returns-export`), cut from and merged into the base branch.
`dcp branch-name` produces the name from a ticket. `dcp branch-share` refuses a name that breaks the pattern,
references a ticket from another project, or references a ticket that was not created for this flow (an epic,
risk or dependency does not count), so a branch can always be traced back to its requirement.

**Build from the shared branch.** `dcp build-brief` assembles the Stage 4 brief from the confirmed pack,
the tickets (with their Pulse keys), the repo rules file, the pattern files, the client constraints
and the tier rules, and tells Claude Code to leave an assumptions note and run `/parakh` before the PR.
The tech lead runs it (the playbook keeps that with them); the command to run is printed when the branch
is shared. If `git.pattern_files` is empty the brief says so loudly instead of guessing a style.

## Is the call ready to draft from?

Before any model drafts a pack, `dcp assess-call` (plain code, no tokens) checks that the call covers the six things a
requirement needs: the job in the client's words, who and how often, what happens today, what done looks like,
the boundaries, and the date. It needs 5 of 6 (`lint.call_ready_min_topics`). A call that does not is a
follow-up call, not a requirement: no draft is queued, a small model posts a request for a follow-up call, and
`dcp next` shows the delivery manager as the person to act. The delivery manager can still draft anyway with
`dcp call-proceed --reason "..."` (the reason is recorded; anyone else is refused). `dcp tick` runs the
assessment itself. The keywords err towards "not found" on purpose: a false "found" would hide a real gap.

## Paste mode: how transcripts arrive

Transcripts are pasted, not fetched. Granola's connector returns a transcript only on a paid tier, and the AI summary
is a paraphrase, so **the summary is never a source**. Each build project has an inbox:

```
data/<KEY>/inbox/            paste a transcript here as a .txt or .md file (see _HOW_TO_USE.md in it)
data/<KEY>/inbox/processed/  filed away once taken in
data/<KEY>/<flow>/transcript.txt   what the checks and the drafter read
```

```bash
python3 -m dcp ingest --project SP      # or just: python3 -m dcp tick --project SP
```

Granola's copy header (`Meeting Title:`, `Date:`, `Meeting participants:`, `Transcript:`) is read and removed; without
it the file name is the title. Rules, none of them configurable:

- **A summary is refused.** Text that is mostly bullets or headings with nobody speaking is rejected with the reason.
- **Under 100 words is refused.** It is not a call.
- **The same call pasted twice is recognised** (by content, whatever the formatting) and filed away, not drafted twice.
- **A note tagged for another project is refused,** so a call never lands on the wrong board.
- A refused file stays in the inbox with the reason until someone fixes it, and `dcp ingest` exits non-zero so a script notices.
- A title that does not read `Project :: topic :: KEY` is accepted but flagged for rename.

`assess-call` applies the same source test to a transcript placed straight in a flow folder, so a summary cannot slip in
by that route either. In paste mode (`granola.source = "paste"`, the default) nothing polls Granola, which also removes
that polling cost; set `"connector"` to read from a Granola folder once transcripts are available that way.

## Models and tokens

Most of the system is plain code and costs nothing: the checks, drift, gates, scheduling, scoring, branch rules
and brief assembly. A model is used only to draft text and to reach the Granola, Slack and Pulse connectors.
Each step has a **kind** and a **tier**:

| Kind | Steps | Model |
|---|---|---|
| code | pack check, ticket check, drift check | never |
| connector | polls, gate prompts, nudges, creating tickets in Pulse, posting the brief | smallest tier while a Claude session is the only way to reach the connectors; `none` once direct API access exists (`runner.mode = "api"`) |
| ai | draft the pack, redraft after a failed check, draft tickets, draft a support ticket, word the client's questions | `small`, `standard` or `strong`, your choice |

```bash
python3 -m dcp models SP                                  # the table, per project
python3 -m dcp models SP --set draft_pack=strong          # change a step; refused if it makes no sense
python3 -m dcp models SP --escalate-after 3
python3 -m dcp next --project SP                          # every order shows its tier and model
python3 -m dcp tick --project SP                          # runs the code steps now; says which model sessions remain
python3 -m dcp ai-used --project SP --flow F --action draft_pack --model sonnet --input-tokens 9200 --output-tokens 2300
python3 -m dcp tokens --project SP                        # tokens by step, by model, per flow, and draft pass rate
```

A tier is a size, not a model name: `config/rules.json` maps `small`, `standard` and `strong` to model aliases,
so changing the model behind a tier is one edit. **Cheap-first is safe** because every draft is checked in code.
A weak draft cannot pass; it costs a retry, and after `escalate_after_failures` failed checks (default 2) the
redraft order moves up one tier on its own (`inputs.escalated_to` says so).

**The idle-polling problem.** A scheduled Claude task is a full AI session every run, even when nothing is
due, and the scheduler tool has no model setting. `dcp tick` removes that cost: it asks the scheduler what is due,
runs the code-only steps itself (reading `data/<KEY>/<flow>/transcript.txt`, `pack_v<N>.md`, `tickets.md`),
and reports the model sessions still needed. A tick with nothing due, or only code, starts no session and spends
no tokens. `dcp tick --launch` starts one headless `claude -p --model <alias>` per model, but that path is
**experimental and unverified**: it only works if the connectors are available to a headless session on that
machine. Test it with one real tick before relying on it.

**Choosing tiers with numbers.** The runner logs one `ai-used` per model call. `dcp tokens` then shows tokens by
step and model, and how often each model's draft was accepted by the checks (overall and first try). Run one real
transcript through `small`, `standard` and `strong` and compare. The relative cost figures use placeholder weights
in `config/rules.json` (`models.relative_cost`): replace them with real per-token prices before reading anything
into cost. Total tokens per flow also feed `dcp report`, next to compliance and rework.

## Pulse tickets in the agreed structure

Every ticket is **self-contained**: it carries the client's words, the acceptance criteria, the technical
context and the answers the client gave, so someone reading only the Pulse ticket has everything. The
linter blocks a story that has no "Client's words". Never "see the pack".

The Pulse connector can create only `story`, `task` or `bug` issues with a plain-text description. It
cannot yet set an epic type, parent, label, sprint or assignee, so the structure is carried by convention
until the API allows it (the request for the tech lead is in `docs/pulse-api-request.md`):

| In the ticket set | In Pulse today | In Pulse once the API allows it |
|---|---|---|
| EPIC | story titled `EPIC: ...`; a comment lists its children | epic type |
| STORY | story with the full field template | same, with a parent link |
| DEP / RISK / DECISION | task titled `DEP: ...` / `RISK: ...` / `DECISION: ...`, with `Label: dependency` as the first description line | same title **plus** a real label (`dependency` / `risk` / `decision`) |
| Blocked by / Parent epic | a comment with the real keys | issue links and a parent link |
| Size | a comment `Size (tech lead): 5 days` | the estimate field |
| Support request | bug or task titled `[L2] : ...`, parent noted in a comment | same, with a parent link |

The title prefix stays permanently so the naming convention is the same everywhere and easy to read. The
plan already carries `labels` and `desired_type` on each operation, so switching to real fields is a small
change in the runner, not in the ticket set.

Descriptions use the same `Label: value` fields the linters read, so any ticket can be pulled back out of
Pulse and re-checked (`dcp lint-pulse`). Creation is resumable and cannot duplicate: the plan is recomputed
from the log, each ticket carries a `dcp-id` footer, and only the operations still missing are returned.

## Manual mode, people by Slack ID, and what to do when a connection is missing

`dcp worksheet --project KEY` turns every step that is due into a checklist: who does it, the exact message to post or text to paste, and the one
command that records it. It needs no connection. `dcp people KEY add --role ... --name ... --slack-id U01ABC2345 [--github login]` registers a
person; any command that needs to know who is acting takes `--by-id <Slack member ID>`. If a person cannot be matched automatically, the system
asks for their Slack member ID (profile, three dots, Copy member ID) rather than guessing. See `docs/HANDOVER.md` for the full fallback table.

## Change requests and follow-up calls

A client changes the requirement after the pack exists, or a second call adds to it. `dcp cr-open --flow F --reason "..." --by-id <delivery manager>`
or a pasted transcript whose first line is `Flow: SP-REQ-001` reopens the pack. The pack is revised (prompt `prompts/stage2b_revise_pack.md`), Gate 1 and
Gate 2 are redone **for the new version** (an approval of the old version cannot be reused), tickets are revised (`prompts/stage3b_revise_tickets.md`:
never reorder, add at the end, mark removals `### REMOVED:`), and `dcp tickets-plan` produces change notes for tickets whose content changed, removal notes, and
creates for new ones. If the build had already started, the developer is told once. Each change request counts as rework in the efficiency mapping.

## Server, webhooks, dashboard and the GitHub-side checks

```bash
export DCP_GITHUB_SECRET=... DCP_SLACK_SIGNING_SECRET=... DCP_API_TOKEN=...
python3 -m dcp serve --port 8080          # /health, /api/next, /dashboard, /webhooks/github, /webhooks/slack
python3 -m dcp dashboard --project SP --out dashboard.html      # one self-contained file, also served at /dashboard
python3 -m dcp webhook-test --project SP --event pull_request --file payload.json     # see what a GitHub payload would log
python3 -m dcp repo-check --path ../the-repo --repo owner/name --github   # CLAUDE.md, CODEOWNERS, PR template, CI, branch protection
python3 -m dcp pr-check --event "$GITHUB_EVENT_PATH" --key SP --base main   # the required status check, for CI
```

Webhooks are refused until their secret is set and unless the signature verifies; the server will not listen beyond localhost without a token. The shipped
PR template (`templates/pull_request_template.md`) carries placeholders only, so an unfilled template fails the check.

## Storage, integrity and governance extras

`storage.backend` is `jsonl` (pilot) or `sqlite` (a team); `dcp migrate-store SP --to sqlite` moves a log with every hash unchanged. Publish `dcp log-head` daily and
check with `dcp verify-log STORE --anchor N:HASH`, so even a consistent rewrite of history is detectable. Scores can be disputed (`dcp dispute`) and are resolved by a
practice lead (`dcp dispute-resolve`), never by the person scored. A real emergency uses `dcp breakglass`, reviewed by someone else within 24 hours. Hours and rework
come in from Pulse exports with `dcp import-hours` and `dcp import-rework` (idempotent).

## Try it

```bash
python3 -m unittest discover -s tests -t .                      # 320 tests

# lint a pack against its call, then the tickets, then drift
python3 -m dcp lint-pack examples/pack_good.md --transcript examples/transcript.txt
python3 -m dcp lint-pack examples/pack_bad.md  --transcript examples/transcript.txt
python3 -m dcp lint-tickets examples/tickets_good.md --stage ready
python3 -m dcp check-drift examples/pack_good.md examples/tickets_good.md

# governance, on SYNTHETIC demo data
python3 examples/make_sample_events.py examples/events_sample.jsonl
python3 -m dcp score  examples/events_sample.jsonl --flow SP-REQ-010
python3 -m dcp report examples/events_sample.jsonl
python3 -m dcp verify-log examples/events_sample.jsonl
```

Add `--log-to <store> --flow <id> --project <key> --actor <who>` to any lint command to write the result
into the event log. Exit code is 1 when anything blocks, so CI and bots can use it directly.

## How a flow is scored

Each check ends as `pass`, `fail`, `na` (does not apply to this track or change) or `pending` (not due
yet). Score = weight of passes / weight of passes + fails. Pending work never counts against anyone,
and a check only fails once its deadline has passed. Every result lists the event ids behind it, so a
score can always be explained and disputed.

Two tracks share the engine: `build` (call to release) and `support` (L2, incidents, hotfixes). Checks
that do not apply to a track show as `na`.

## Mapping compliance to efficiency

`dcp report` relates each flow's compliance to rework events, hours overrun and lead time, and compares
high-compliance flows (80+) with the rest. It refuses to conclude with fewer than 3 flows, labels results
"indicative only" under 8, and always says correlation is not proof. To claim a gain, compare against a
pre-rollout baseline from Pulse history.

`examples/events_sample.jsonl` is invented. The link between compliance and rework in it is built in on
purpose to show the mechanism. It says nothing about real teams.

## Known limits

- Quote checking is textual. It proves the client's words exist in the transcript, not that the
  AI understood them. A person still reads the pack (Gate 1).
- The six-must-haves coverage check (PK017) is a keyword heuristic. It only warns.
- Drift similarity is lexical. A criterion reworded in a way that keeps the words but flips the meaning
  can slip through; the Given/When/Then split narrows this but does not remove it.
- Pulse structure is by convention (see above) until the API exposes epics, parents and labels.
- Slack reactions carry no timestamp through the connector, so gate review time is accurate only to the poll interval.
- Scores are only as good as the events fed in. A step nobody logs shows as pending, then overdue.

## Not built yet (for the tech team; see docs/HANDOVER.md and docs/BACKLOG.md)

1. **Pulse API adapter** (the current connector cannot set labels, epics, parents, links, sprints, estimates, or update an issue).
2. **Slack app** that turns verified reactions and replies into approvals (signature checks and the handshake are done).
3. **GitHub App, required status check and branch protection** (event mapping, the PR check and the readiness check are done).
4. **Granola transcript access** by API (needs a paid tier; paste mode works today and the summary is never used).
5. **Drafting through the model API** with a model per step (the plumbing, tiers, escalation and token log are done).
6. **Hosting, secrets, TLS, backups, dashboard login, per-person views.**
7. **A pilot on real client calls**, the model bake-off, and a measured baseline.
8. **Verifying** `dcp tick --launch`, the Docker files and the Action template, none of which has been run.

## Adding a check or an event

- New event type: add it to `EVENT_TYPES` in `dcp/events.py` with its required keys.
- New compliance check: add a function with `@check("C20", "name", tracks)` in `dcp/scoring.py`, then
  an entry under `compliance.checks` in `config/rules.json`.
- New lint rule: add the finding in the relevant linter; its severity can be overridden per rule id
  under `lint.severity_overrides` (`block`, `warn` or `off`).
