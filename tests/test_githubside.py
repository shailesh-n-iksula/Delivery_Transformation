import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dcp.cli import main
from dcp.prcheck import check_pr, render as render_pr
from dcp.repocheck import check_repo, render, summarise

ROOT = Path(__file__).resolve().parent.parent
SHA = "c" * 40
TEMPLATE = (ROOT / "templates" / "pull_request_template.md").read_text()


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            code = main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


def by_id(results, cid):
    return next(r for r in results if r["id"] == cid)


class RepoCheckTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def ready_repo(self):
        self.write("CLAUDE.md", "# House rules\nStack: Magento 2.4.6 on PHP 8.2.\n" + "Module map: Checkout is owned by team A, Payments by team B, Sync by team C. " * 6)
        self.write(".github/CODEOWNERS", "/app/code/Payment/ @arjunk @samr\n/app/code/Checkout/ @arjunk @samr\n")
        self.write(".github/pull_request_template.md", TEMPLATE)
        self.write(".github/workflows/ci.yml", "jobs:\n  test:\n    steps:\n      - run: composer test\n      - run: composer audit\n      - run: phpstan\n  delivery-check:\n    steps: []\n")
        self.write("docs/TECH_DEBT.md", "# debt\n")
        self.write("tests/a.php", "x")
        self.write(".gitignore", ".env\nvendor/\n")

    def test_an_empty_repo_fails_every_required_check_with_a_fix(self):
        res = check_repo(self.root)
        self.assertFalse(summarise(res)["ready"])
        for cid in ("R01", "R02", "R03", "R04", "R08"):
            r = by_id(res, cid)
            self.assertEqual(r["status"], "fail", cid)
            self.assertTrue(r["fix"], cid)

    def test_a_prepared_repo_is_ready_apart_from_what_only_github_can_say(self):
        self.ready_repo()
        res = check_repo(self.root)
        self.assertTrue(all(r["status"] == "pass" for r in res if r["id"] != "R10"), [r for r in res if r["status"] == "fail"])
        self.assertEqual(by_id(res, "R10")["status"], "not checked")      # never reported as a pass
        s = summarise(res)
        self.assertEqual((s["ready"], s["unchecked"]), (False, ["R10"]))

    def test_a_stub_house_rules_file_is_not_accepted(self):
        self.ready_repo()
        self.write("CLAUDE.md", "# Rules\nBe careful.\n")
        self.assertIn("stub", by_id(check_repo(self.root), "R01")["detail"])
        self.write("CLAUDE.md", "# Rules\n" + "Write good code and be careful with the customer data at all times. " * 10)
        self.assertIn("no versions or modules", by_id(check_repo(self.root), "R01")["detail"])

    def test_codeowners_needs_two_named_owners_for_the_senior_rule(self):
        self.ready_repo()
        self.write(".github/CODEOWNERS", "# only comments\n* @arjunk\n")
        r = by_id(check_repo(self.root), "R02")
        self.assertEqual(r["status"], "fail")
        self.assertIn("two named approvers", r["fix"])

    def test_a_pr_template_missing_fields_names_them(self):
        self.ready_repo()
        self.write(".github/pull_request_template.md", "## Summary\n")
        r = by_id(check_repo(self.root), "R03")
        self.assertEqual(r["status"], "fail")
        for label in ("Ticket:", "AI-assisted:", "parakh:"):
            self.assertIn(label, r["detail"])

    def test_ci_without_tests_and_an_unignored_env_file_fail(self):
        self.ready_repo()
        self.write(".github/workflows/ci.yml", "jobs:\n  build:\n    steps:\n      - run: echo hi\n")
        self.write(".gitignore", "vendor/\n")
        res = check_repo(self.root)
        self.assertEqual((by_id(res, "R04")["status"], by_id(res, "R08")["status"]), ("fail", "fail"))

    def github(self, payload=None, code=0, err=""):
        calls = []

        def runner(args):
            calls.append(args)
            return SimpleNamespace(returncode=code, stdout=json.dumps(payload or {}), stderr=err)
        return runner, calls

    def test_branch_protection_that_is_properly_set_passes(self):
        self.ready_repo()
        runner, calls = self.github({"required_pull_request_reviews": {"required_approving_review_count": 1, "dismiss_stale_reviews": True},
                                     "required_status_checks": {"contexts": ["delivery-check / pr-check"]}, "enforce_admins": {"enabled": True}})
        res = check_repo(self.root, "main", "iksula/x", True, runner)
        self.assertEqual(by_id(res, "R10")["status"], "pass")
        self.assertEqual(calls[0], ["gh", "api", "repos/iksula/x/branches/main/protection"])
        self.assertTrue(summarise(res)["ready"])

    def test_each_branch_protection_gap_is_named(self):
        self.ready_repo()
        runner, _ = self.github({"required_pull_request_reviews": {"required_approving_review_count": 0}, "enforce_admins": {"enabled": False}})
        d = by_id(check_repo(self.root, "main", "iksula/x", True, runner), "R10")["detail"]
        for needle in ("fewer than 1 approval", "stale approvals", "no required status checks", "admins can bypass"):
            self.assertIn(needle, d)

    def test_an_unprotected_branch_fails_and_an_unreachable_github_is_not_a_pass(self):
        self.ready_repo()
        runner, _ = self.github(code=1, err="gh: Branch not protected (HTTP 404)")
        self.assertEqual(by_id(check_repo(self.root, "main", "iksula/x", True, runner), "R10")["status"], "fail")
        runner, _ = self.github(code=1, err="gh auth login required")
        self.assertEqual(by_id(check_repo(self.root, "main", "iksula/x", True, runner), "R10")["status"], "not checked")

    def test_the_command_exit_code_and_output(self):
        self.ready_repo()
        code, out = run("repo-check", "--path", str(self.root))
        self.assertEqual(code, 1)                        # R10 is unchecked, so not ready
        self.assertIn("R10", out)
        self.assertIn("NOT READY", out)
        code, out = run("repo-check", "--path", str(self.root), "--json")
        self.assertFalse(json.loads(out)["summary"]["ready"])
        self.assertIn("fix:", render(check_repo(self.root / "nope")))


def event(body, ref="feature/SP-2-returns-export", sha=SHA, base="main"):
    return {"pull_request": {"body": body, "head": {"ref": ref, "sha": sha}, "base": {"ref": base}}}


GOOD = f"""Ticket: SP-2
Tier: T2
AI-assisted: yes
I can explain every line: [x]
<!-- parakh: {{"gate": "pass", "head_sha": "{SHA}"}} -->"""


class PrCheckTests(unittest.TestCase):
    def fails(self, ev):
        return [r["label"] for r in check_pr(ev, "SP", "main") if not r["ok"]]

    def test_a_complete_t2_pr_passes(self):
        self.assertEqual(self.fails(event(GOOD)), [])

    def test_the_shipped_template_left_unfilled_fails_every_declaration(self):
        res = check_pr(event(TEMPLATE), "SP", "main")
        bad = {r["label"] for r in res if not r["ok"]}
        for label in ("Names its ticket, and it matches the branch", "Declares AI use (yes or no)", "Declares a tier",
                      "Maker confirms they can explain every line", "Parakh verdict pasted", "T1: failure-path test described", "T1: rollback note"):
            self.assertIn(label, bad)

    def test_the_unfilled_template_on_a_bad_branch_fails_with_a_fix_for_each_gap(self):
        res = check_pr(event(TEMPLATE, ref="my-fix", base="develop"), "SP", "main")
        bad = {r["label"]: r for r in res if not r["ok"]}
        for label in ("Branch follows GitHub flow", "Targets 'main'", "Declares AI use (yes or no)",
                      "Maker confirms they can explain every line"):
            self.assertIn(label, bad)
            self.assertTrue(bad[label]["fix"], label)

    def test_a_ticket_that_disagrees_with_the_branch_or_the_project_fails(self):
        self.assertIn("Names its ticket, and it matches the branch", self.fails(event(GOOD.replace("SP-2", "SP-9"))))
        self.assertIn("Branch follows GitHub flow", self.fails(event(GOOD, ref="feature/ML-2-x")))

    def test_a_stale_parakh_verdict_is_caught(self):
        r = self.fails(event(GOOD, sha="d" * 40))
        self.assertIn("Parakh verdict is for the latest commit", r)
        self.assertIn("Parakh gate is a pass", [x for x in self.fails(event(GOOD.replace('"pass"', '"fail"')))])

    def test_no_parakh_verdict_at_all_fails_except_for_t3(self):
        no_footer = GOOD.split("<!--")[0]
        self.assertIn("Parakh verdict pasted", self.fails(event(no_footer)))
        self.assertEqual(self.fails(event(no_footer.replace("T2", "T3"))), [])

    def test_t1_needs_a_failure_path_test_and_a_rollback_note(self):
        t1 = GOOD.replace("T2", "T1")
        self.assertEqual(sorted(self.fails(event(t1))), ["T1: failure-path test described", "T1: rollback note"])
        full = t1 + "\nFailure-path test: gateway timeout in tests/TimeoutTest.php\nRollback note: revert and flush cache"
        self.assertEqual(self.fails(event(full)), [])

    def test_silence_about_the_tier_is_treated_strictly(self):
        silent = GOOD.replace("Tier: T2\n", "")
        f = self.fails(event(silent))
        self.assertIn("Declares a tier", f)
        self.assertIn("T1: rollback note", f)

    def test_the_command_reads_an_event_file_and_sets_the_exit_code(self):
        d = tempfile.TemporaryDirectory()
        p = Path(d.name) / "event.json"
        p.write_text(json.dumps(event(GOOD)))
        self.assertEqual(run("pr-check", "--event", str(p), "--key", "sp")[0], 0)
        p.write_text(json.dumps(event("")))
        code, out = run("pr-check", "--event", str(p), "--key", "SP")
        self.assertEqual(code, 1)
        self.assertIn("fix:", out)
        self.assertIn("FAIL", render_pr(check_pr(event(""), "SP", "main")))
        d.cleanup()

    def test_the_shipped_workflow_template_calls_the_check(self):
        t = (ROOT / "templates" / "github-workflow-delivery-check.yml").read_text()
        self.assertIn("python3 -m dcp pr-check", t)
        self.assertIn("TEMPLATE. Not run or tested", t)


if __name__ == "__main__":
    unittest.main()
