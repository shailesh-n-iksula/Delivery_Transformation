# Deploying

Everything here is a **starting point that has not been run**: Docker was not available where it was written, and
no GitHub, Slack or Pulse credentials exist yet. Treat it as a checklist of what a deployment needs.

| Need | Where | Notes |
|---|---|---|
| The server (webhooks, orders, dashboard) | `Dockerfile`, `docker-compose.yml` `server` | Refuses to bind publicly without `DCP_API_TOKEN`. Put TLS in front. |
| The scheduler | `docker-compose.yml` `ticker`, or `crontab.example` | `dcp tick` runs the code-only steps itself. |
| Secrets | `.env.example` | Use a secrets manager. The webhook secrets and the API token are required; the PLACEHOLDER lines are for adapters not built yet. |
| Storage | `projects/` and `data/` volumes | Move a project to SQLite with `dcp migrate-store SP --to sqlite`. Back `data/` up. |
| Webhooks | GitHub repo or App settings; Slack app | See `docs/ADAPTERS.md` for the events to subscribe to. |

Before real client data goes in: read `docs/SECURITY.md`.
