# Open questions

Each has the default that was assumed, so nothing is blocked. Change the default by changing the setting named.
**Confirmed by the owner on 2026-10-25:** 1 (cloud account), 3 (practice lead per project), 4 (kept for the life of the project; deletion on client request is still to be built), 5 (managers see roles only; no per-person view yet). The rest are still open.

| # | Question | Assumed for now | Setting |
|---|---|---|---|
| 1 | Where will it run (cloud account, internal server)? **Confirmed: a cloud account Iksula already uses.** | One small VM or container for the pilot | `deploy/` |
| 2 | Which database for the team? | Text file for the pilot, SQLite for a team, Postgres later behind the same interface | `storage.backend` |
| 3 | Who is the practice lead who resolves score disputes? **Confirmed: one practice lead per project.** | One named per project | `people.practice_lead` |
| 4 | How long are transcripts and logs kept? **Confirmed: the life of the project.** Deletion when a client asks is still to be built | No automatic deletion yet | none yet |
| 5 | Who sees the dashboard? **Confirmed: managers (delivery manager, tech lead, practice lead), roles not names.** Whether people also see their own view is still open | No per-person view yet | `dashboard.py` |
| 6 | Does a change request need the client's written agreement attached? | The delivery manager opens it and states the client's words in the reason | `cmd_cr_open` |
| 7 | Who owns Parakh's output footer and the pull request template? | The Parakh owners add the footer; each repo copies the template | `ADAPTERS.md` |
| 8 | One Slack channel per project, or shared channels? | One per project (`slack.channel`) | project file |
| 9 | One repo per project, or several (monorepo, separate front end)? | One repo per project | `git.repo` |
| 10 | Are nudges sent outside working hours? Which time zone? | No working-hours rule yet: nudges fire when the wait passes the SLA | `schedules.gate_nudge_hours` |
| 11 | Is the 5-of-6 call-readiness threshold right for your calls? | 5 | `lint.call_ready_min_topics` |
| 12 | Which period gives the measured baseline? (Pulse history before rollout) | Not decided | `baselines` |
| 13 | Is the 60-second Gate 1 and Gate 2 review floor right? | 60 seconds | `compliance.checks.C05.params` |
| 14 | What do support tickets need beyond the fields already required (SLA, severity matrix)? | Priority guessed from keywords, tier suggested, both confirmed by a person | `supportlint.py` |
| 15 | Which approved model account and region may client data go to? | None chosen; drafting is done by a person or a Claude session the owner uses | `SECURITY.md` item 11 |
| 16 | Does Pulse have sprints the tickets should land in automatically? | Tickets go to the backlog | `docs/pulse-api-request.md` |
| 17 | Will other tools than Pulse, Slack, GitHub and Granola be needed (Jira, Teams, GitLab)? | No; only the adapters in `ADAPTERS.md` | adapters |
