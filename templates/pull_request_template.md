<!--
Pull request template for every repo that uses the delivery system.
The delivery system and the CI check read the lines below, so keep the labels exactly as they are.
Every value in <angle brackets> must be replaced. A pull request with an unfilled template is refused by the check.
-->

Ticket: <PULSE-KEY from the branch name, e.g. SP-123>
Tier: <T1, T2 or T3, as set by the tech lead on the ticket>
AI-assisted: <yes or no>
I can explain every line: [ ]

## What this changes and why
<one or two sentences, in plain words>

## Tier T1 only (money, data, auth, orders, sync, patches, production config)
Failure-path test: <what happens when the gateway, ERP or network fails, and which test proves it>
Rollback note: <exactly how to undo this in production>
Staging evidence: <what you ran and what you saw>

## Assumptions made that the ticket did not state
<link to docs/assumptions/<flow>.md, or write None>

## Review
Run /parakh before raising this pull request. Paste the footer it prints on the next line, replacing the values.
Do not remove the comment markers. A footer for an older commit does not count.

<!-- parakh: {"gate": "<pass or fail>", "head_sha": "<full commit sha>", "tier": "<T1, T2 or T3>", "critical": 0, "major": 0} -->
