# Requirement Pack: Returns export
Project: SP
Call: SITEONE Pimcore :: returns export :: SP
Version: 1

## 1. One-line summary
"A store manager cannot find last month's returns without exporting everything"

## 2. Context
Store managers (about forty) export the full returns grid from the Magento admin at month end and filter it by hand in Excel. Finance reads a nightly file from an SFTP folder. Volume is about 2,000 returns a day, 8,000 on the worst day, and about 40,000 rows a month.

## 3. Scope - in
1. Add a returns export button with date and store filters to the Magento admin. [Q: "an export button with date and store filters in the Magento admin"]
2. Generate a nightly returns CSV of about 40,000 rows and deliver it to SFTP. [Q: "a nightly CSV job of about 40,000 rows to SFTP"]
3. Restrict the export to store managers. [Q: "Only store managers should see it, not the support agents"]

## 4. Scope - out
1. Any change to the existing returns grid. [Q: "We must not change the existing returns grid"]

## 5. Acceptance criteria
1. Given a store manager is logged in to the Magento admin, When they choose last month and one store and click export, Then a CSV downloads containing only that store's returns for that month. [Q: "pick last month, filter to one store, and compare the count with what finance has"]
2. Given the export has run, When the row count is compared with finance's count for the same store and month, Then the two counts match. [Q: "If it matches, we are done"]
3. Given the nightly job has run, When finance opens the SFTP folder, Then a CSV with the same columns as today's file is present. [Q: "the nightly file must keep the same columns"]
4. Given a support agent is logged in, When they open the returns area, Then the export is not visible. [Q: "Only store managers should see it, not the support agents"]

## 6. Assumptions
1. Cancelled orders are excluded from the export until the client confirms.
2. The nightly file replaces the one finance reads today, with the same name pattern and location.

## 7. Open questions
1. Which SFTP target should the nightly file go to, and who owns it? [Owner: Priya (client IT)]
2. Should cancelled orders be included? [Owner: Priya]
3. Does Rahul approve the file format? [Owner: Rahul (finance)]

## 8. Risks and dependencies
1. RISK: 40,000 rows may time out in the admin export. [Owner: Tech lead] [By: 2026-11-06]
2. DEP: Client to confirm the SFTP target and credentials. [Owner: Priya] [By: 2026-11-04]
3. DECISION: Cancelled orders included or not. [Owner: Priya] [By: 2026-11-04]

## 9. Effort shape
Size: 8-12 days. Uncertain: SFTP access and nightly job volume. This is a shape for the tech lead, not a quote.
