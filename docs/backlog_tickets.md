### EPIC: Take the delivery control plane from tested core to production
Why: The tested core makes the playbook enforceable and measurable, but nothing is connected to Pulse, Slack or GitHub, and nothing has run on a real client call. The team needs these built before the system can be called final.
Links: Requirement Pack: docs/HANDOVER.md (what exists and what is missing); Granola call: written from the owner's messages in the build session
Answers from the client: The owner said the tech team will add the Pulse API, finalise GitHub, and sort the connections.

### STORY: Build the Pulse API adapter that creates, links, updates and reads tickets
Why: Tickets must be created in the agreed structure with no hand work. Today the connector cannot set labels, epics, parents, links, sprints or estimates, or update an issue.
Scope in:
1. Execute the operations from the ticket plan against the Pulse API, including labels, epic type, parent and issue links.
2. Search Pulse for the dcp-id footer before creating, so a crash never duplicates a ticket.
3. Apply a requirement change as an update, and read worklogs and reopened tickets back as events.
Scope out:
1. Replacing the manual worksheet fallback.
Acceptance criteria:
1. Given a confirmed ticket set, When the adapter runs the plan, Then every ticket exists in Pulse with its label, parent and links set and its key recorded.
2. Given the process stopped after creating a ticket but before recording it, When the adapter runs again, Then no second ticket is created.
3. Given a requirement change, When the adapter runs the change operation, Then the existing ticket's description is updated and the change is noted in a comment.
Client's words: "tech team will add the API so that its seamless"; "tickets created on pulse should have all the information"
Technical context: Pulse API for issues, comments, labels, links and worklogs; about 40 tickets per requirement and 3 requirements a week per project, within the API's per-minute and per-hour write limits.
Links: Requirement Pack: docs/BACKLOG.md; Granola call: written from the owner's messages; Epic: Take the delivery control plane from tested core to production
Answers from the client: The title prefixes RISK: and DEP: stay as well as the label.
Assumptions: The API exposes everything in docs/pulse-api-request.md.
Blocked by: DEP: Tech lead to expose the Pulse API features in the request

### STORY: Build the Slack app and map gate replies and reactions to approvals
Why: Approvals and replies must come from verified people in Slack with no typing of IDs, so the delivery manager and tech lead can act without leaving Slack.
Scope in:
1. Post gate prompts, nudges and follow-up requests from the existing templates, and record the message ids.
2. Turn a verified reaction or thread reply into the same approval, sizing, tier or branch record the commands make.
3. Ask for a Slack member ID when a person is not recognised, and never approve on a guess.
Scope out:
1. Model-written messages for gates and nudges.
Acceptance criteria:
1. Given a gate prompt and a tick from the registered delivery manager, When the event arrives, Then Gate 1 is approved only if the pack passed its checks and the open questions were sent.
2. Given a tick from a person who is not registered, When the event arrives, Then nothing is approved and the thread asks for their Slack member ID.
3. Given the same event delivered twice, When the adapter processes both, Then only one approval is recorded.
Client's words: "the input should be slack ID if it is not found automatically"
Technical context: Slack app with event subscriptions and the Web API; one channel per project and about 30 events a day per project; reactions carry no timestamp.
Links: Requirement Pack: docs/ADAPTERS.md; Granola call: written from the owner's messages; Epic: Take the delivery control plane from tested core to production
Answers from the client: Slack ID is the fallback input when a person is not found automatically.
Assumptions: One channel per project.
Blocked by: DEP: Slack workspace admin to approve the app

### STORY: Finalise the GitHub App, the required status check and branch protection
Why: Pull requests must be tied to tickets, carry the declarations and a current Parakh verdict, and be independently approved. That needs GitHub enforcing it, not goodwill.
Scope in:
1. Register the GitHub App or webhooks and point them at the server with the secret.
2. Install the delivery check and make it a required status check on the base branch.
3. Turn on branch protection and have the repo readiness check pass on each pilot repo.
Scope out:
1. Non-GitHub hosts.
Acceptance criteria:
1. Given a pull request with the template unfilled, When the check runs, Then the pull request cannot merge.
2. Given a pull request with a Parakh verdict for an older commit, When it is pushed to, Then the verdict is stale and the check fails until it is renewed.
3. Given a pilot repo, When the readiness check runs with GitHub access, Then every required check passes including branch protection.
Client's words: "my git is connected to claude, But share a not to the tech team so that they can take care of this before finalising the system"
Technical context: GitHub App with pull request, review and comment webhooks; one repo per project and about 10 pull requests a week; the Parakh output must carry the machine-readable footer.
Links: Requirement Pack: docs/ADAPTERS.md; Granola call: written from the owner's messages; Epic: Take the delivery control plane from tested core to production
Answers from the client: GitHub is connected for the owner; the tech team finalises it.
Assumptions: GitHub flow only.
Blocked by: DEP: GitHub organisation admin to install the app; RISK: Parakh output has no machine-readable footer yet

### STORY: Run drafting through the model API with a model chosen per step
Why: Drafting should not need a person to copy text between tools, and token use must stay low by choosing the model size per step.
Scope in:
1. Call the model API for each order that needs a model, using the model named on the order, and escalate when told.
2. Log tokens from each response with the existing command.
3. Run the bake-off protocol on pilot transcripts and set real prices.
Scope out:
1. Letting a model approve any gate.
Acceptance criteria:
1. Given an idle project, When the tick runs, Then no model is called.
2. Given a draft that fails its checks twice, When the redraft is ordered, Then it runs on the next tier up.
3. Given any model call, When it finishes, Then its tokens are recorded against the step and the model.
Client's words: "user can choose the models at each steps so that the token consumption is not high"
Technical context: A model API account approved for client data; about 5 drafts a day per project, each a transcript of up to 20,000 tokens in and 3,000 tokens out.
Links: Requirement Pack: docs/ADAPTERS.md; Granola call: written from the owner's messages; Epic: Take the delivery control plane from tested core to production
Answers from the client: The owner wants model choice per step, like the Brain and Spine.
Assumptions: Tiers small, standard and strong map to the models in config/rules.json.
Blocked by: DEP: Legal and client sign-off for sending call content to a model

### STORY: Host the server and scheduler with secrets, TLS, backups and a login for the dashboard
Why: The system must run without a laptop, keep its log safe, and show the dashboard only to the right people.
Scope in:
1. Run the server and the ticker from the deploy files, with secrets in a secrets manager and TLS in front.
2. Back up the data folder and test a restore.
3. Put a login in front of the dashboard and publish the log head hash daily.
Scope out:
1. Per-person dashboard views.
Acceptance criteria:
1. Given a restart, When the ticker comes back, Then no step is skipped or repeated.
2. Given the dashboard URL without a login, When it is opened, Then access is refused.
3. Given the log rewritten consistently, When the daily check runs against the published head hash, Then it fails.
Client's words: "what is missing to make this system running"
Technical context: One small server running the Python server and a five-minute ticker in Docker; a database growing by about 3,000 events a month per project; the deploy files have never been run.
Links: Requirement Pack: docs/SECURITY.md; Granola call: written from the owner's messages; Epic: Take the delivery control plane from tested core to production
Answers from the client: None
Assumptions: SQLite is enough for the pilot.
Blocked by: DECISION: Where the system is hosted

### STORY: Run a pilot on five real client calls and measure against a baseline
Why: Nothing has run on a real client requirement call, and the efficiency claim needs a measured baseline.
Scope in:
1. Run five real requirement calls end to end and record every miss.
2. Import hours and rework from Pulse and build the baseline from earlier projects.
3. Review the dashboard with the people who will be scored.
Scope out:
1. Scoring individuals before the baseline exists.
Acceptance criteria:
1. Given five finished pilot flows, When the report runs, Then it shows compliance against rework, hours and lead time with the sample size stated.
2. Given a miss the checks did not catch, When it is found, Then a new check or an accepted limitation is recorded.
3. Given the people being scored, When they review the dashboard, Then they know how to dispute a result.
Client's words: "map that against the efficiency gained"; "check if what I ahve requested is being followed or not diligently"
Technical context: The Pulse API for earlier worklogs and reopened tickets; 5 flows of up to 40 tickets each.
Links: Requirement Pack: docs/TEST_PLAN.md; Granola call: written from the owner's messages; Epic: Take the delivery control plane from tested core to production
Answers from the client: Governance is for both coaching and performance.
Assumptions: Five calls from two projects is enough for a first read.
Blocked by: DECISION: How long transcripts and logs are kept

### DEP: Tech lead to expose the Pulse API features in the request
Owner: Tech lead
Needed by: 2026-11-14
What stops if late: The Pulse adapter cannot set labels, epics or links, so tickets stay in the workaround structure.
Blocks: Build the Pulse API adapter that creates, links, updates and reads tickets

### DEP: Slack workspace admin to approve the app
Owner: Slack workspace admin
Needed by: 2026-11-14
What stops if late: Gate replies cannot be read from Slack, so approvals stay manual.
Blocks: Build the Slack app and map gate replies and reactions to approvals

### DEP: GitHub organisation admin to install the app
Owner: GitHub organisation admin
Needed by: 2026-11-14
What stops if late: The required check and branch protection cannot be enforced.
Blocks: Finalise the GitHub App, the required status check and branch protection

### DEP: Legal and client sign-off for sending call content to a model
Owner: Practice head
Needed by: 2026-11-21
What stops if late: Drafting must stay manual in an assistant approved for client data.
Blocks: Run drafting through the model API with a model chosen per step

### RISK: Parakh output has no machine-readable footer yet
Impact: The check cannot tell whether a verdict is current, so the Parakh check stays a manual step.
Mitigation: Agree the footer in docs/ADAPTERS.md with the Parakh owners and test it on the pilot repos.
Owner: Tech lead
Review date: 2026-11-14

### DECISION: Where the system is hosted
Who decides: Head of delivery
Options: A cloud account the team already uses; an internal server.
Cost of deciding late: The deploy files, secrets and backups cannot be settled, which delays every adapter's go-live.
Status: Open

### DECISION: How long transcripts and logs are kept
Who decides: Practice head
Options: The life of the project; a fixed period such as 12 months; deleted on client request.
Cost of deciding late: Personal data is kept without a policy, and deletion has to be built after the fact.
Status: Open
