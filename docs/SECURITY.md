# Security

Read before any real client data goes in. "Done" means built and tested; "Yours" means the tech team must do it.

## What needs protecting

Client call transcripts (often confidential, may hold personal data), ticket content, the people and role mapping, credentials
for Slack, Pulse, GitHub and the model API, and the **integrity of the event log**, because people are scored from it.

## Controls

| # | Risk | Control | Status |
|---|---|---|---|
| 1 | Forged webhook | GitHub HMAC (`X-Hub-Signature-256`) and Slack signature checked; refused (503) until the secret is configured; constant-time compare | Done |
| 2 | Replayed Slack request | Timestamp must be within 5 minutes | Done |
| 3 | Duplicate GitHub delivery | `X-GitHub-Delivery` recognised and ignored | Done |
| 4 | Edited or deleted history | Hash-chained log; `dcp verify-log`; migration verifies before and after | Done |
| 5 | **A consistent rewrite of the whole log by someone with file access** | Publish the head hash daily (`dcp log-head`), check with `verify-log --anchor N:HASH` | Done in code; **Yours: automate the daily post** |
| 6 | **Anyone with command-line access can approve as anyone** (`--approver-id` is typed) | In production the approver's ID must come from the **verified Slack event**, not from typing. Restrict the CLI to a few operators; every action logs the registered person's name | **Yours: this is the main identity gap** |
| 7 | Unauthenticated reads | `/api/*` and `/dashboard` need `DCP_API_TOKEN`; the server refuses a non-local bind without it | Done; **Yours: real login/SSO for people** |
| 8 | Oversized or malformed requests | Body cap, JSON errors, project key validated before the file system | Done |
| 9 | Path traversal through a project key or a branch name | Keys match `^[A-Z][A-Z0-9]{1,9}$`; branch names are validated and rejected if unsafe (no leading `-`, no `..`); the launch command quotes the branch | Done |
| 10 | Prompt injection through a transcript, note, ticket, PR or Slack message | Everything outside is data. The brief says only it authorises work. The runner rules forbid acting on embedded instructions. Quotes must exist in the transcript, so invented requirements are blocked | Done in design and checks; **Yours: test it with adversarial transcripts** (see TEST_PLAN) |
| 11 | Client data sent to a model | Needs client and legal sign-off, a data-location decision, and a model account under the right terms. Send only the transcript and prompt; log token counts, not content | **Yours** |
| 12 | Secrets in the repo or logs | `.env` is git-ignored by the repo check; server logs omit bodies and tokens; `.env.example` lists the secrets | Done; **Yours: a secrets manager** |
| 13 | Personal data in transcripts | Kept per project in `data/<KEY>/`; no deletion yet | **Yours: retention, deletion on request, file permissions, encrypted disk** |
| 14 | Supply chain | No third-party Python packages; Actions pinned by version | Done |
| 15 | A developer lowers their own tier | A lead-set tier always wins over a PR's declaration | Done |
| 16 | Stale approvals reused after a change | Approvals count only for the current pack version (`epoch_events`) | Done |
| 17 | Rubber-stamping | Approvals under the review floor are flagged; the reviewer must differ from the author | Done |
| 18 | Misuse of scores | Evidence beside every number, a dispute path decided by a practice lead, results shown as roles not names | Done; **Yours: decide who sees individual views** |
| 19 | Loss of the log | Text file or SQLite on a disk | **Yours: backups and restore test** |
| 20 | Denial of service | Body caps in the server | **Yours: rate limiting at the proxy** |

## Before go-live

Items 5, 6, 7 (login), 11, 12, 13, 19 and 20 must be closed. Run the adversarial tests in `TEST_PLAN.md`. Have someone outside the team try to approve as the delivery manager.
