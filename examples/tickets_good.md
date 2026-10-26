### EPIC: Returns export
Why: Store managers lose an afternoon each month filtering a full returns export by hand, and finance needs a stable nightly file before the 1 December audit.
Links: Requirement Pack SP-REQ-001 v1; Granola call "SITEONE Pimcore :: returns export :: SP"

### STORY: Add returns export button with date and store filters to Magento admin
Why: Store managers cannot find last month's returns without exporting everything. A filtered export removes the manual Excel step.
Scope in:
1. Export button with date and store filters on the returns area.
Scope out:
1. Any change to the existing returns grid.
Acceptance criteria:
1. Given a store manager is logged in to the Magento admin, When they choose last month and one store and click export, Then a CSV downloads containing only that store's returns for that month.
2. Given the export has run, When the row count is compared with finance's count for the same store and month, Then the two counts match.
Client's words: "an export button with date and store filters in the Magento admin"; "pick last month, filter to one store, and compare the count with what finance has"
Technical context: Magento admin, new action beside the returns grid, no change to the grid itself. Export covers up to 40,000 rows a month, about 2,000 returns a day.
Links: Requirement Pack SP-REQ-001 v1; Granola call SITEONE returns export; Epic: Returns export
Answers from the client: None yet. Whether cancelled orders are included is tracked as a DECISION ticket.
Assumptions: Cancelled orders are excluded until the client confirms.
Blocked by: RISK: 40,000 rows may time out in the admin export
Size: 5 days (tech lead)

### STORY: Generate nightly returns CSV job and deliver it to SFTP for finance
Why: Finance picks the file up from an SFTP folder every night and needs the same columns as today.
Scope in:
1. Nightly returns CSV of about 40,000 rows delivered to SFTP.
Scope out:
1. Any change to the existing returns grid.
Acceptance criteria:
1. Given the nightly job has run, When finance opens the SFTP folder, Then a CSV with the same columns as today's file is present.
Client's words: "a nightly CSV job of about 40,000 rows to SFTP"; "the nightly file must keep the same columns"
Technical context: Magento cron job reading the returns table, 40,000 rows nightly, writes CSV to the client SFTP server.
Links: Requirement Pack SP-REQ-001 v1; Granola call SITEONE returns export; Epic: Returns export
Answers from the client: None yet. The SFTP target is tracked as a DEP ticket.
Assumptions: The nightly file replaces the one finance reads today, same name pattern and location.
Blocked by: DEP: Client to confirm the SFTP target and credentials; DECISION: Cancelled orders included or not
Size: 3 days (tech lead)

### STORY: Restrict returns export to store managers in Magento admin
Why: Only store managers should see the export. Support agents must not.
Scope in:
1. Export visible to the store manager role only.
Scope out:
1. Any change to the existing returns grid.
Acceptance criteria:
1. Given a support agent is logged in, When they open the returns area, Then the export is not visible.
Client's words: "Only store managers should see it, not the support agents"
Technical context: Magento admin ACL resource and role for about 40 users (store managers).
Links: Requirement Pack SP-REQ-001 v1; Granola call SITEONE returns export; Epic: Returns export
Answers from the client: None
Assumptions: None
Blocked by: none
Size: 1 day (tech lead)

### DEP: Client to confirm the SFTP target and credentials
Owner: Priya (client IT)
Needed by: 2026-11-04
What stops if late: The nightly job cannot be tested end to end.
Blocks: Generate nightly returns CSV job and deliver it to SFTP for finance

### RISK: 40,000 rows may time out in the admin export
Impact: The export button would fail for the largest stores at month end.
Mitigation: Stream the CSV in batches and test with 40,000 rows on staging.
Owner: Tech lead
Review date: 2026-11-06

### DECISION: Cancelled orders included or not
Who decides: Priya (client)
Options: Include with a status column; exclude.
Cost of deciding late: Changes the export query and the finance reconciliation after build.
Status: Closed
