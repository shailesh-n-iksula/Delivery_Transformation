"""Is a repo ready for this process? Checks the files Parakh and the delivery system rely on, and, when the GitHub
command-line tool is available and signed in, branch protection. Anything it cannot check is reported as 'not checked',
never as a pass."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

REQUIRED, RECOMMENDED = "required", "recommended"
PASS, FAIL, UNKNOWN = "pass", "fail", "not checked"
PR_LABELS = ("Ticket:", "Tier:", "AI-assisted:", "Rollback note:", "Failure-path test:", "parakh:")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _first(root: Path, *names) -> Optional[Path]:
    for n in names:
        p = root / n
        if p.is_file():
            return p
    return None


def check_repo(root: Path, base_branch: str = "main", repo: str = "", github: bool = False,
               runner: Optional[Callable] = None) -> List[dict]:
    root = Path(root)
    out: List[dict] = []

    def add(cid, level, status, label, detail, fix=""):
        out.append({"id": cid, "level": level, "status": status, "label": label, "detail": detail, "fix": fix})

    # house rules the AI and Parakh both read
    cm = _first(root, "CLAUDE.md")
    text = _read(cm) if cm else ""
    if not cm:
        add("R01", REQUIRED, FAIL, "House rules file (CLAUDE.md)", "missing", "Run /parakh-init, then fill in the stack, versions, module map and fragile areas.")
    elif len(text.strip()) < 400:
        add("R01", REQUIRED, FAIL, "House rules file (CLAUDE.md)", f"only {len(text.strip())} characters: a stub", "Fill in the exact stack and versions, the module map (who owns checkout, payments, sync) and the known fragile areas.")
    elif not re.search(r"\d+\.\d+|version", text, re.I) or not re.search(r"module|owner", text, re.I):
        add("R01", REQUIRED, FAIL, "House rules file (CLAUDE.md)", "present, but it names no versions or modules", "Add the exact stack and versions and a module map.")
    else:
        add("R01", REQUIRED, PASS, "House rules file (CLAUDE.md)", f"{len(text.strip())} characters")

    # two named approvers for sensitive paths
    co = _first(root, ".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
    rules = [l for l in _read(co).splitlines() if l.strip() and not l.strip().startswith("#")] if co else []
    owners = {o for l in rules for o in l.split()[1:] if o.startswith("@")}
    if not co:
        add("R02", REQUIRED, FAIL, "CODEOWNERS", "missing", "Name the T1 paths (payments, checkout, sync, auth, patches) and two approvers for them.")
    elif len(owners) < 2:
        add("R02", REQUIRED, FAIL, "CODEOWNERS", f"{len(rules)} rule(s) and {len(owners)} named owner(s)", "A T1 path needs two named approvers, so the senior-approver rule has something to apply to.")
    else:
        add("R02", REQUIRED, PASS, "CODEOWNERS", f"{len(rules)} rules, {len(owners)} named owners")

    # the template the PR check reads
    pt = _first(root, ".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md", "pull_request_template.md", "docs/pull_request_template.md")
    if not pt:
        add("R03", REQUIRED, FAIL, "Pull request template", "missing", "Copy templates/pull_request_template.md from the delivery system to .github/pull_request_template.md.")
    else:
        missing = [l for l in PR_LABELS if l.lower() not in _read(pt).lower()]
        add("R03", REQUIRED, FAIL if missing else PASS, "Pull request template",
            ("missing: " + ", ".join(missing)) if missing else "has every field the delivery system reads",
            "Use templates/pull_request_template.md as it is." if missing else "")

    wf = sorted((root / ".github" / "workflows").glob("*.y*ml")) if (root / ".github" / "workflows").is_dir() else []
    wtext = " ".join(_read(p) for p in wf).lower()
    if not wf:
        add("R04", REQUIRED, FAIL, "CI workflow", "no workflow files", "Add CI that runs the tests, the linter and a dependency audit on every pull request.")
    else:
        found = [k for k in ("test", "lint", "phpstan", "eslint", "phpcs", "audit") if k in wtext]
        add("R04", REQUIRED, PASS if ("test" in found) else FAIL, "CI workflow",
            f"{len(wf)} workflow(s); mentions {', '.join(found) or 'nothing recognisable'}", "" if "test" in found else "Run the test suite in CI.")
        add("R05", RECOMMENDED, PASS if "delivery" in wtext or "pr-check" in wtext else FAIL, "Delivery PR check in CI",
            "found" if ("delivery" in wtext or "pr-check" in wtext) else "none",
            "" if ("delivery" in wtext or "pr-check" in wtext) else "Add templates/github-workflow-delivery-check.yml, then make it a required status check.")
    add("R06", RECOMMENDED, PASS if (root / "docs" / "TECH_DEBT.md").is_file() else FAIL, "Technical debt ledger (docs/TECH_DEBT.md)",
        "present" if (root / "docs" / "TECH_DEBT.md").is_file() else "missing", "" if (root / "docs" / "TECH_DEBT.md").is_file() else "Run /parakh-init: deferred work needs a ticket and an owner.")
    has_tests = any((root / d).is_dir() for d in ("tests", "test", "__tests__", "spec", "Test", "dev/tests"))
    add("R07", RECOMMENDED, PASS if has_tests else FAIL, "A tests folder", "found" if has_tests else "none found", "" if has_tests else "Parakh reads tests; it does not write them.")
    gi = _read(root / ".gitignore")
    add("R08", REQUIRED, PASS if re.search(r"^\s*\.env\b", gi, re.M) else FAIL, "Secrets kept out of the repo (.env ignored)",
        "ignored" if re.search(r"^\s*\.env\b", gi, re.M) else ".env is not in .gitignore", "" if re.search(r"^\s*\.env\b", gi, re.M) else "Add .env to .gitignore and rotate anything already committed.")
    dep = (root / ".github" / "dependabot.yml").is_file() or (root / ".github" / "dependabot.yaml").is_file() or "audit" in wtext
    add("R09", RECOMMENDED, PASS if dep else FAIL, "Dependency and security updates", "found" if dep else "none", "" if dep else "Enable Dependabot or a dependency audit in CI.")

    # branch protection needs GitHub itself
    if not github or not repo:
        add("R10", REQUIRED, UNKNOWN, f"Branch protection on '{base_branch}'", "needs GitHub access",
            "A repo admin must turn on: pull request required, 1+ approval, dismiss stale approvals, required status checks, no direct pushes. "
            "Rerun with --github when the GitHub command-line tool is signed in.")
    else:
        run = runner or (lambda args: subprocess.run(args, capture_output=True, text=True, timeout=30))
        if not shutil.which("gh") and runner is None:
            add("R10", REQUIRED, UNKNOWN, f"Branch protection on '{base_branch}'", "the 'gh' command is not installed", "Install and sign in to the GitHub CLI, or check in the repository settings.")
        else:
            res = run(["gh", "api", f"repos/{repo}/branches/{base_branch}/protection"])
            if res.returncode != 0:
                msg = (res.stderr or res.stdout or "").strip()
                if "404" in msg or "not protected" in msg.lower():
                    add("R10", REQUIRED, FAIL, f"Branch protection on '{base_branch}'", "the branch is not protected", "Turn on branch protection (a repo admin; see the checklist in docs/HANDOVER.md).")
                else:
                    add("R10", REQUIRED, UNKNOWN, f"Branch protection on '{base_branch}'", "could not ask GitHub: " + msg[:120], "Sign in to the GitHub CLI or check the repository settings.")
            else:
                try:
                    p = json.loads(res.stdout)
                except json.JSONDecodeError:
                    p = {}
                reviews = p.get("required_pull_request_reviews") or {}
                problems = []
                if not reviews:
                    problems.append("no pull request review required")
                elif reviews.get("required_approving_review_count", 0) < 1:
                    problems.append("fewer than 1 approval required")
                if reviews and not reviews.get("dismiss_stale_reviews"):
                    problems.append("stale approvals are not dismissed on new pushes")
                if not ((p.get("required_status_checks") or {}).get("contexts") or (p.get("required_status_checks") or {}).get("checks")):
                    problems.append("no required status checks")
                if (p.get("enforce_admins") or {}).get("enabled") is False:
                    problems.append("admins can bypass it")
                add("R10", REQUIRED, FAIL if problems else PASS, f"Branch protection on '{base_branch}'", "; ".join(problems) or "pull request, approval, stale-approval and status-check rules are on",
                    "Fix these in the repository settings." if problems else "")
    return out


def summarise(results: List[dict]) -> dict:
    req = [r for r in results if r["level"] == REQUIRED]
    return {"ready": all(r["status"] == PASS for r in req),
            "required_passed": sum(1 for r in req if r["status"] == PASS), "required_total": len(req),
            "unchecked": [r["id"] for r in results if r["status"] == UNKNOWN]}


def render(results: List[dict]) -> str:
    mark = {PASS: "PASS", FAIL: "FAIL", UNKNOWN: "?   "}
    lines = []
    for r in results:
        lines.append(f"  {mark[r['status']]} {r['id']} [{r['level']}] {r['label']}: {r['detail']}")
        if r["status"] != PASS and r["fix"]:
            lines.append(f"       fix: {r['fix']}")
    s = summarise(results)
    lines.append("")
    lines.append(f"{s['required_passed']} of {s['required_total']} required checks pass"
                 + (f"; {len(s['unchecked'])} could not be checked ({', '.join(s['unchecked'])})" if s["unchecked"] else "")
                 + (". READY." if s["ready"] else ". NOT READY."))
    return "\n".join(lines)
