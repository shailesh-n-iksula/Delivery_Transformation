The confirmed Requirement Pack changed to version <n+1> after tickets already exist in Pulse. Revise the ticket set.

Inputs: the previous ticket set (the one in Pulse), the new confirmed pack, and its section 10 "Changes in this version".

Produce the ticket set in the same structure as `prompts/stage3_ticket_set.md`, with these extra rules. The tickets in
Pulse are matched to this set by ORDER and WORDING, so the rules below are what keep a revision from looking like a
rewrite:

- Keep the EPIC, every STORY, DEP, RISK and DECISION that did not change, in the SAME ORDER and with EXACTLY the same text.
- Put NEW stories, dependencies, risks and decisions at the END of their kind, after the existing ones. Never insert
  in the middle and never renumber.
- A CHANGED story keeps its position and its title; only the changed fields (criteria, scope, client's words, answers)
  are edited. Copy changed criteria word for word from the pack.
- A story that was REMOVED from the pack is not deleted from the set. Add it at the very end as:

```
### REMOVED: <the exact title it had>
Why: <why it was removed, from section 10 of the pack>
```

- Acceptance criteria are still copied from the pack word for word. Do not set a Size.
