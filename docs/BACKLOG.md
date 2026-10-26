# Build backlog for the tech team

The same backlog in the system's own ticket format is in `backlog_tickets.md`: one epic, stories with acceptance criteria and the owner's
own words, and the dependencies, risks and decisions. It passes `python3 -m dcp lint-tickets docs/backlog_tickets.md`, so once Pulse write
access exists it can be created with the same plan machinery the system uses for client work.

Suggested order:

1. **Foundations in parallel:** Pulse API request to the tech lead; Slack app approval; GitHub App registration; security decisions (model account, retention, hosting).
2. **Adapters:** Pulse, then Slack, then GitHub. Keep the manual fallback working at each step.
3. **Hosting:** server, ticker, secrets, backups, TLS, dashboard login.
4. **Drafting through the model API** with per-step model choice and token logging.
5. **Pilot** on 5 real calls (`TEST_PLAN.md`), then the bake-off and the baseline.
6. **Close** the security items, then call it final against the checklist in `HANDOVER.md`.
