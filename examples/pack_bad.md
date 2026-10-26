# Requirement Pack: Returns export

## 1. One-line summary
Returns export for store managers.

## 2. Context
Managers export returns.

## 3. Scope - in
1. Add a returns export button with date and store filters to the Magento admin. [Q: "an export button with date and store filters in the Magento admin"]
2. Build a real-time returns analytics dashboard.
3. Send a weekly email summary of returns. [Q: "returns should be fast and delight users"]

## 4. Scope - out
Nothing.

## 5. Acceptance criteria
1. The export should be fast and robust.
2. Given a manager clicks export, When the file is ready, Then it downloads properly. [Q: "If it matches, we are done"]
3. Store managers can see returns.

## 7. Open questions
1. Which SFTP target should we use?

## 8. Risks and dependencies
1. RISK: Export may time out. [Owner: Tech lead]
2. Client to confirm SFTP.

## 9. Effort shape
We guarantee delivery in 5 days.
