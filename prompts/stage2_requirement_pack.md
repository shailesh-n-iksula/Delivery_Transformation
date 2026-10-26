Read my Granola meeting note titled <exact title> (or: use the transcript below).

Produce a Requirement Pack in EXACTLY this markdown structure. The linter (`dcp lint-pack`) reads it,
so do not rename headings or change the tag formats.

```
# Requirement Pack: <short name>
Project: <Pulse project key>
Call: <exact Granola note title>
Version: 1

## 1. One-line summary
"<the job to be done, in the client's own words, in double quotes>"

## 2. Context
<current process, systems touched, volumes, who uses it>

## 3. Scope - in
1. <shippable item> [Q: "<client's exact words from the call that justify this item>"]

## 4. Scope - out
1. <what we are explicitly not doing>

## 5. Acceptance criteria
1. Given <state>, When <action>, Then <observable result>. [Q: "<client's exact words>"]

## 6. Assumptions
1. <anything you had to assume because the call did not say it>

## 7. Open questions
1. <what you could not determine> [Owner: <who must answer>]

## 8. Risks and dependencies
1. RISK: <what could go wrong> [Owner: <name>] [By: YYYY-MM-DD]
2. DEP: <what we need, from whom> [Owner: <name>] [By: YYYY-MM-DD]
3. DECISION: <open question that blocks the build> [Owner: <name>] [By: YYYY-MM-DD]

## 9. Effort shape
Size: <rough size in days>. Uncertain: <which parts>. This is a shape for the tech lead, not a quote.
```

Rules:
- Use only what was said on the call. If it was not said, it goes in Assumptions or Open questions, never in Scope.
- Every Scope-in item and every Acceptance criterion MUST carry a [Q: "..."] tag. The quote must be copied
  verbatim from the transcript, 3 words or more. If you cannot find words to quote, the item is not scope.
- Acceptance criteria must be observable by someone who was not on the call. Never use words like
  "robust", "fast", "user-friendly", "properly", "seamless".
- Every Open question needs an [Owner: ...]. Every Risk, Dependency and Decision needs [Owner: ...] and [By: YYYY-MM-DD].
- Flag any place where two people on the call said different things, as an Open question.
- List which of these the call did NOT cover, as Open questions: job to be done, who and how often, what happens
  today, what done looks like, boundaries, date and reason.
- Do not give a price or a delivery commitment.
