"""A small reference server: webhooks in, orders and the dashboard out. Standard library only.

It is deliberately conservative, so the tech team inherits safe defaults:
  * a webhook is refused (503) until its secret is configured, and refused (401) unless its signature verifies;
  * Slack requests older than five minutes are refused as replays; GitHub redeliveries are recognised and ignored;
  * bodies are capped; project keys are validated before they touch the file system;
  * it binds to localhost, and refuses to bind elsewhere without an API token for the read endpoints.
Configuration comes from the environment: DCP_GITHUB_SECRET, DCP_SLACK_SIGNING_SECRET, DCP_API_TOKEN.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .events import open_store
from .flowstate import next_orders
from .project import ProjectError, load_project, project_path, store_path, validate_project
from .webhooks import process_github, process_slack, verify_github, verify_slack

_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
LOOPBACK = ("127.0.0.1", "localhost", "::1")


@dataclass
class Context:
    projects_dir: Path
    data_dir: Path
    rules: dict
    github_secret: str = ""
    slack_secret: str = ""
    api_token: str = ""
    max_body: int = 1_000_000
    clock: Optional[float] = None       # tests only


def context_from_env(projects_dir, data_dir, rules) -> Context:
    return Context(Path(projects_dir), Path(data_dir), rules, os.environ.get("DCP_GITHUB_SECRET", ""),
                   os.environ.get("DCP_SLACK_SIGNING_SECRET", ""), os.environ.get("DCP_API_TOKEN", ""))


def _json(status: int, obj) -> Tuple[int, str, bytes]:
    return status, "application/json", json.dumps(obj, indent=2).encode()


def _project(ctx: Context, key: str):
    if not _KEY.match((key or "").upper()):
        raise ProjectError("a valid project key is required")
    cfg = load_project(project_path(ctx.projects_dir, key))
    problems = validate_project(cfg)
    if problems:
        raise ProjectError("the project is not ready: " + "; ".join(problems[:3]))
    return cfg, open_store(store_path(ctx.data_dir, cfg["key"], cfg["storage"]["backend"]))


def _project_for_repo(ctx: Context, repo: str) -> Optional[str]:
    for p in sorted(Path(ctx.projects_dir).glob("*.json")):
        try:
            if (load_project(p).get("git") or {}).get("repo", "").lower() == repo.lower():
                return p.stem
        except ProjectError:
            continue
    return None


def _authorised(ctx: Context, headers: Dict[str, str]) -> bool:
    if not ctx.api_token:
        return True
    given = headers.get("authorization", "")
    return given.startswith("Bearer ") and hmac.compare_digest(given[7:], ctx.api_token)


def handle(ctx: Context, method: str, target: str, headers: Dict[str, str], body: bytes = b"") -> Tuple[int, str, bytes]:
    headers = {k.lower(): v for k, v in headers.items()}
    url = urlparse(target)
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    path = url.path.rstrip("/") or "/"
    if len(body) > ctx.max_body:
        return _json(413, {"error": "body too large"})
    try:
        if method == "GET" and path == "/health":
            return _json(200, {"ok": True, "projects": sorted(p.stem for p in Path(ctx.projects_dir).glob("*.json"))})
        if method == "GET" and path in ("/api/next", "/dashboard", "/api/worksheet"):
            if not _authorised(ctx, headers):
                return _json(401, {"error": "missing or wrong bearer token"})
            cfg, store = _project(ctx, q.get("project", ""))
            if path == "/dashboard":
                from .dashboard import render_dashboard
                return 200, "text/html; charset=utf-8", render_dashboard(cfg, store, ctx.rules).encode()
            orders, waiting = next_orders(store, cfg, None, ctx.rules)
            return _json(200, {"project": cfg["key"], "needs_ai": any(o.needs_ai for o in orders),
                               "orders": [o.to_dict() for o in orders], "waiting": [w.to_dict() for w in waiting]})
        if method == "POST" and path == "/webhooks/github":
            if not ctx.github_secret:
                return _json(503, {"error": "GitHub webhook secret is not configured, so webhooks are refused"})
            if not verify_github(ctx.github_secret, body, headers.get("x-hub-signature-256")):
                return _json(401, {"error": "signature does not verify"})
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return _json(400, {"error": "body is not JSON"})
            event = headers.get("x-github-event", "")
            if event == "ping":
                return _json(200, {"pong": True})
            key = q.get("project") or _project_for_repo(ctx, (payload.get("repository") or {}).get("full_name", ""))
            if not key:
                return _json(404, {"error": "no project is registered for this repository"})
            cfg, store = _project(ctx, key)
            return _json(200, process_github(event, payload, cfg, store, headers.get("x-github-delivery", "")))
        if method == "POST" and path == "/webhooks/slack":
            if not ctx.slack_secret:
                return _json(503, {"error": "Slack signing secret is not configured, so webhooks are refused"})
            if not verify_slack(ctx.slack_secret, headers.get("x-slack-request-timestamp"), body,
                                headers.get("x-slack-signature"), ctx.clock):
                return _json(401, {"error": "signature does not verify or the request is too old"})
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return _json(400, {"error": "body is not JSON"})
            out = process_slack(payload)
            if "challenge" in out:
                return _json(200, {"challenge": out["challenge"]})
            raw = Path(ctx.data_dir) / "_slack_events.jsonl"
            raw.parent.mkdir(parents=True, exist_ok=True)
            with open(raw, "a", encoding="utf-8") as f:
                f.write(json.dumps({"received": time.time(), "payload": payload}) + "\n")
            return _json(200, {"ok": True, "stub": True})
        if path in ("/health", "/api/next", "/dashboard", "/api/worksheet", "/webhooks/github", "/webhooks/slack"):
            return _json(405, {"error": "method not allowed"})
        return _json(404, {"error": "not found"})
    except ProjectError as e:
        return _json(404 if "no project file" in str(e) else 400, {"error": str(e)})


def make_server(ctx: Context, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    if host not in LOOPBACK and not ctx.api_token:
        raise SystemExit("refusing to listen on a non-local address without DCP_API_TOKEN set: the read endpoints "
                         "would be open to anyone who can reach this machine")

    class Handler(BaseHTTPRequestHandler):
        def _go(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, ctx.max_body + 1)) if n else b""
            status, ctype, out = handle(ctx, method, self.path, dict(self.headers), body)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(out)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, fmt, *args):      # no request bodies or tokens in logs
            pass

    return ThreadingHTTPServer((host, port), Handler)
