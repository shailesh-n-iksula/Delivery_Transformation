The requirement changed after the pack was drafted. Revise the Requirement Pack.

Inputs: the current pack (version <n>), the full transcript (which now includes any follow-up call, marked
"=== FOLLOW-UP CALL: ... ==="), and the reason it was reopened: <reason, in the client's own words>.

Produce version <n+1> of the pack in EXACTLY the same structure as `prompts/stage2_requirement_pack.md`, with
`Version: <n+1>` in the header, plus one extra final section:

```
## 10. Changes in this version
- ADDED scope 4: <what> [Q: "<client's words>"]
- CHANGED criterion 2: <what changed and why> [Q: "<client's words>"]
- REMOVED scope 3: <what and why> [Q: "<client's words>"]
```

Rules:
- Keep every item that did not change EXACTLY as it was, with the same number and wording. The tickets already in Pulse
  are matched to the pack by position and wording, so reordering or rewording unchanged items creates false changes.
- Add new items at the END of their list. Never renumber.
- A removed item stays in the list marked `(REMOVED in v<n+1>)` and is listed under section 10.
- Every ADDED or CHANGED scope item and criterion needs a [Q: "..."] tag with the client's exact words from the
  transcript, including the follow-up call. If the client did not say it, it is an Assumption or an Open question.
- Anything the follow-up call contradicts goes into Open questions, with the two versions of what was said.
