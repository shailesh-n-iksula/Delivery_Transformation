Here is the CONFIRMED Requirement Pack: <paste>.
Project: <Pulse project key>. Platform: <stack>. Sprint length: <n> weeks.

Draft the ticket set in EXACTLY this markdown structure. The linter (`dcp lint-tickets`) and the drift check
(`dcp check-drift`) read it.

```
### EPIC: <title>
Why: <business reason>
Links: Requirement Pack <id> v<n>; Granola call "<title>"

### STORY: <verb + object + context>
Why: <two lines from the pack's context. The business reason, not the feature.>
Scope in:
1. <copied from the pack, not rewritten>
Scope out:
1. <copied from the pack>
Acceptance criteria:
1. Given ..., When ..., Then ...   <copied from the pack, wording unchanged>
Client's words: <the [Q: "..."] quotes from the pack that this story came from, verbatim, separated by ";">
Technical context: <the actual systems, modules, data sources, volumes and the client's stated constraint>
Links: Requirement Pack <id> v<n>; Granola call <title>; Epic: <epic title>
Answers from the client: <answers the client gave to open questions that affect this story, or None>
Assumptions: <from the pack, or None>
Blocked by: <exact titles of DEP/RISK/DECISION tickets below, separated by ";", or none>

### DEP: <what we need, from whom>
Owner: <name>
Needed by: YYYY-MM-DD
What stops if late: <plain words>
Blocks: <story title>

### RISK: <what could go wrong>
Impact: <plain words>
Mitigation: <what we will do>
Owner: <name>
Review date: YYYY-MM-DD

### DECISION: <the open question>
Who decides: <name>
Options: <the options>
Cost of deciding late: <plain words>
Status: Open
```

Rules:
- Acceptance criteria are copied from the pack word for word. Do not add, drop or reword any. If something is
  missing from the pack, list it under "Missing from the pack" at the end instead of inventing a criterion.
- One story per shippable slice. One DEP/RISK/DECISION ticket per item in the pack's section 8.
- Do not set a Size. The tech lead sizes at Gate 2.
- Every ticket must stand alone: someone reading only the Pulse ticket must have the client's words, the criteria,
  the technical context and the answers. Never write "see the pack" instead of the content.
- Technical context must name real systems ("Magento admin", not "the back end") and state volumes.
