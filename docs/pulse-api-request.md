# Pulse API request for the tech lead

Purpose: let the delivery control system create tickets in the agreed structure with no hand editing.
Today the Pulse connector can create only `story`, `task` and `bug` issues, with a plain-text description.
Everything below is what is missing. Nothing here changes how people use Pulse.

## What we need

| # | Capability | Why | Today's workaround |
|---|---|---|---|
| 1 | **Set labels on create and update** (`risk`, `dependency`, `decision`) | Risks and dependencies are tasks with a label, so they can be filtered and reported | `Label: risk` as the first line of the description, plus the title prefix |
| 2 | **Epic issue type, and set a parent / epic link** on create | Epic, then story or task, then risk or dependency | Epic created as a story titled `EPIC: ...`; parent written in a comment |
| 3 | **Issue links** (`blocks` / `is blocked by`) between issues | A story must be blocked by its open risks and dependencies | A comment on each ticket listing the keys |
| 4 | **Assign to a sprint** on create or update | Tickets land on the right board without a manual move | Tickets go to the backlog |
| 5 | **Set assignee** on create or update | Owner on every dependency and risk | Owner written in the description |
| 6 | **Estimate / story points field** writable | The tech lead's size should live in the ticket | `Size (tech lead): 5 days` as a comment |
| 7 | **Update issue** (title, description, priority) | Re-sync when the confirmed requirement changes | Not possible; a new comment is added |
| 8 | **Worklog read** scope (`read:worklogs`) | Estimate vs actual hours, the margin half of the governance view | Not available |

Scopes to grant on the integration: `write:issues`, `write:comments`, `read:worklogs`, plus whatever the
label, link, sprint and assignee operations need.

## Separate request: workflow rules

Pulse workflows cannot be changed through the API today. If the tech lead can change them in the admin UI,
the single highest-value rule is: **a ticket cannot move to In Progress unless it has a description with
acceptance criteria and an assignee**. Without it, the system can only detect and comment on a ticket that
skipped the Definition of Ready, not stop it.

## Ready-to-send message

> Hi, we are building an automated flow from client call to Pulse tickets to a Git branch. It creates epics,
> stories, and risk/dependency tasks in a fixed structure. The Pulse connector we have can only create
> story/task/bug with a plain description. Could you expose, in the Pulse API or the integration scopes:
> labels (set on create/update), an epic type with parent/epic link, issue links (blocks / is blocked by),
> sprint assignment, assignee, a writable estimate field, update-issue, and read access to worklogs? A
> workflow rule that blocks moving a ticket to In Progress without acceptance criteria and an assignee would
> also help. Details and reasons are in the attached table.
