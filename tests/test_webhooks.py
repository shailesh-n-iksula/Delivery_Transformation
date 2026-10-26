import hashlib
import hmac
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dcp.config import load_rules
from dcp.events import open_store
from dcp.project import load_project
from dcp.scoring import evaluate_store
from dcp.server import Context, handle, make_server
from dcp.webhooks import parse_pr_body, process_github, process_slack, verify_github, verify_slack

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules()
SHA1, SHA2 = "a" * 40, "b" * 40
GOOD_BODY = f"""Ticket: SP-2
Tier: T2
AI-assisted: yes
I can explain every line: [x]

## Tier T1 only
Failure-path test: gateway timeout test in tests/Payment/TimeoutTest.php
Rollback note: revert the merge and flush the cache

<!-- parakh: {{"gate": "pass", "head_sha": "{SHA1}", "tier": "T2", "critical": 0, "major": 0}} -->
"""


def pr(action="opened", number=7, ref="feature/SP-2-returns-export", sha=SHA1, author="dev1", body=GOOD_BODY, base="main",
       merged=False, merged_by=None, repo="iksula/siteone-pimcore"):
    p = {"action": action, "number": number, "repository": {"full_name": repo},
         "pull_request": {"number": number, "user": {"login": author}, "head": {"ref": ref, "sha": sha}, "base": {"ref": base},
                          "body": body, "merged": merged}}
    if merged_by:
        p["pull_request"]["merged_by"] = {"login": merged_by}
    return p


def review(state="approved", reviewer="dev2", number=7, sha=SHA1, ref="feature/SP-2-returns-export", repo="iksula/siteone-pimcore"):
    return {"action": "submitted", "repository": {"full_name": repo}, "review": {"state": state.upper(), "user": {"login": reviewer}, "commit_id": sha},
            "pull_request": {"number": number, "head": {"ref": ref, "sha": sha}, "base": {"ref": "main"}, "user": {"login": "dev1"}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "projects").mkdir()
        cfg = json.loads((ROOT / "projects" / "SP.json").read_text())
        cfg["people"]["tech_lead"]["github"] = "arjunk"
        cfg["people"]["delivery_manager"]["github"] = "meeran"
        cfg["people"]["senior"] = {"name": "Sam R", "slack_id": "U0SENIOR01", "email": "", "github": "samr"}
        (d / "projects" / "SP.json").write_text(json.dumps(cfg))
        self.d, self.cfg = d, load_project(d / "projects" / "SP.json")
        self.store = open_store(d / "data" / "SP" / "events.jsonl")
        self.store.append("call.recorded", "SP-REQ-001", "SP", "a", {"title": "S :: t :: SP"})
        self.store.append("pulse.mapped", "SP-REQ-001", "SP", "a", {"local_id": "s1", "key": "SP-2"})
        self.store.append("tier.set", "SP-REQ-001", "SP", "a", {"tier": "T2", "actor_role": "tech_lead"})

    def tearDown(self):
        self.dir.cleanup()

    def gh(self, event, payload, delivery=""):
        return process_github(event, payload, self.cfg, self.store, delivery)

    def types(self, flow="SP-REQ-001"):
        return [e.type for e in self.store.events(flow=flow)]

    def check(self, cid, flow="SP-REQ-001"):
        r = evaluate_store(self.store, RULES, flow=flow, now=datetime.now(timezone.utc) + timedelta(days=1))[0]
        return next(c for c in r.checks if c.id == cid)


class SignatureTests(unittest.TestCase):
    def test_github_signature(self):
        body = b'{"a":1}'
        good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_github("s3cret", body, good))
        self.assertFalse(verify_github("s3cret", body, good[:-1] + "0"))
        self.assertFalse(verify_github("other", body, good))
        self.assertFalse(verify_github("s3cret", body + b" ", good))
        for bad in (None, "", "sha1=abc", good.replace("sha256=", "")):
            self.assertFalse(verify_github("s3cret", body, bad))
        self.assertFalse(verify_github("", body, good))        # no secret configured: nothing verifies

    def test_slack_signature_and_replay_protection(self):
        body, ts = b'{"type":"event_callback"}', "1700000000"
        sig = "v0=" + hmac.new(b"sk", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_slack("sk", ts, body, sig, now=1700000100))
        self.assertFalse(verify_slack("sk", ts, body, sig, now=1700000400))       # older than five minutes
        self.assertFalse(verify_slack("sk", ts, body, sig, now=1699999000))       # from the future
        self.assertFalse(verify_slack("sk", ts, body + b"x", sig, now=1700000100))
        self.assertFalse(verify_slack("sk", "not-a-number", body, sig, now=1700000100))
        self.assertFalse(verify_slack("sk", None, body, sig))
        self.assertFalse(verify_slack("", ts, body, sig, now=1700000100))


class BodyParsingTests(unittest.TestCase):
    def test_a_filled_template(self):
        p = parse_pr_body(GOOD_BODY)
        self.assertEqual((p["ticket"], p["tier"], p["ai_declared"], p["explains_every_line"]), ("SP-2", "T2", True, True))
        self.assertTrue(p["failure_path_test"] and p["rollback_note"])
        self.assertEqual(p["parakh"]["head_sha"], SHA1)

    def test_the_ai_declaration_counts_whether_it_says_yes_or_no_but_not_if_absent(self):
        self.assertTrue(parse_pr_body("AI-assisted: no")["ai_declared"])
        self.assertTrue(parse_pr_body("ai-assisted: Yes, mostly")["ai_declared"])
        self.assertFalse(parse_pr_body("Ticket: SP-2")["ai_declared"])
        self.assertFalse(parse_pr_body("AI-assisted: <yes or no>")["ai_declared"])

    def test_placeholder_evidence_is_not_evidence(self):
        for v in ("", "N/A", "none", "TBD", "<what happens when it fails>"):
            p = parse_pr_body(f"Failure-path test: {v}\nRollback note: {v}")
            self.assertEqual((p["failure_path_test"], p["rollback_note"]), ("", ""), v)

    def test_the_unfilled_shipped_template_declares_nothing(self):
        p = parse_pr_body((ROOT / "templates" / "pull_request_template.md").read_text())
        self.assertEqual((p["ticket"], p["tier"], p["ai_declared"], p["explains_every_line"], p["failure_path_test"], p["rollback_note"]),
                         ("", "", False, False, "", ""))
        self.assertIsNone(p["parakh"])

    def test_a_broken_or_incomplete_parakh_footer_is_ignored(self):
        for footer in ('<!-- parakh: {"gate": "pass", "head_sha": "<full commit sha>"} -->', '<!-- parakh: {"gate": "<pass or fail>", "head_sha": "abc1234"} -->',
                       '<!-- parakh: {"gate": "maybe", "head_sha": "abc1234"} -->', '<!-- parakh: {not json} -->', '<!-- parakh: {"gate": "pass"} -->', '<!-- parakh: {"head_sha": "x"} -->', "no footer"):
            self.assertIsNone(parse_pr_body(footer)["parakh"], footer)

    def test_an_empty_body(self):
        p = parse_pr_body(None)
        self.assertEqual((p["ticket"], p["ai_declared"], p["parakh"]), ("", False, None))


class GitHubMappingTests(Base):
    def test_an_opened_pr_is_tied_to_its_flow_by_the_branch(self):
        r = self.gh("pull_request", pr(), "d1")
        self.assertEqual((r["handled"], r["flow"], r["events"]), (True, "SP-REQ-001", ["pr.opened", "parakh.verdict"]))
        e = self.store.events(type="pr.opened")[0]
        self.assertEqual((e.data["pr"], e.data["author"], e.data["ticket"], e.data["tier"], e.data["branch_problem"]), (7, "dev1", "SP-2", "T2", ""))
        self.assertTrue(e.data["ai_declared"] and e.data["rollback_note"])
        v = self.store.events(type="parakh.verdict")[0]
        self.assertEqual((v.data["gate"], v.data["head_sha"]), ("pass", SHA1))

    def test_a_redelivered_webhook_is_ignored(self):
        self.gh("pull_request", pr(), "same-delivery")
        r = self.gh("pull_request", pr(), "same-delivery")
        self.assertIn("duplicate", r["note"])
        self.assertEqual(self.types().count("pr.opened"), 1)

    def test_a_pr_that_names_no_ticket_is_logged_not_lost_and_fails_the_ticket_check(self):
        r = self.gh("pull_request", pr(ref="my-quick-fix", body="AI-assisted: no"))
        self.assertEqual(r["flow"], "SP-UNLINKED")
        self.assertIn("pr.opened", self.types("SP-UNLINKED"))
        self.assertEqual(self.check("C11", "SP-UNLINKED").status, "fail")
        self.assertEqual(self.check("C21", "SP-UNLINKED").status, "fail")

    def test_a_branch_for_a_ticket_that_is_not_a_story_of_any_flow_is_unlinked(self):
        self.assertEqual(self.gh("pull_request", pr(ref="feature/SP-99-nope"))["flow"], "SP-UNLINKED")

    def test_the_body_cannot_claim_a_different_ticket_than_the_branch(self):
        self.gh("pull_request", pr(body=GOOD_BODY.replace("Ticket: SP-2", "Ticket: SP-77")))
        self.assertEqual(self.store.events(type="pr.opened")[0].data["ticket"], "")
        self.assertEqual(self.check("C11").status, "fail")

    def test_the_wrong_target_branch_is_recorded_and_scored(self):
        self.gh("pull_request", pr(base="develop"))
        self.assertIn("targets 'develop'", self.store.events(type="pr.opened")[0].data["branch_problem"])
        self.assertEqual(self.check("C21").status, "fail")
        self.gh("pull_request", pr(number=8, base="main", sha=SHA2))
        self.assertTrue(self.check("C21").status == "fail")           # one bad PR is enough

    def test_a_good_pr_passes_the_branch_check(self):
        self.gh("pull_request", pr())
        self.assertEqual(self.check("C21").status, "pass")

    def test_a_push_after_the_verdict_makes_the_verdict_stale_until_a_new_one_arrives(self):
        self.gh("pull_request", pr(), "d1")
        self.assertEqual(self.check("C13").status, "pass")
        self.gh("pull_request", pr(action="synchronize", sha=SHA2), "d2")
        self.assertIn("pr.pushed", self.types())
        self.assertEqual(self.check("C13").status, "fail")             # no verdict for the latest commit
        fresh = GOOD_BODY.replace(SHA1, SHA2)
        r = self.gh("pull_request", pr(action="edited", sha=SHA2, body=fresh), "d3")
        self.assertEqual(r["events"], ["parakh.verdict"])
        self.assertEqual(self.check("C13").status, "pass")

    def test_the_same_verdict_is_not_logged_twice(self):
        self.gh("pull_request", pr(), "d1")
        self.gh("pull_request", pr(action="edited"), "d2")
        self.assertEqual(self.types().count("parakh.verdict"), 1)

    def test_a_parakh_footer_in_a_comment_counts_and_finds_its_pr(self):
        self.gh("pull_request", pr(body="Ticket: SP-2\nAI-assisted: yes"), "d1")
        self.assertEqual(self.check("C13").status, "fail")
        comment = {"action": "created", "repository": {"full_name": "iksula/siteone-pimcore"}, "issue": {"number": 7, "pull_request": {}},
                   "comment": {"user": {"login": "dev1"}, "body": f'<!-- parakh: {{"gate": "pass", "head_sha": "{SHA1}"}} -->'}}
        r = self.gh("issue_comment", comment, "d2")
        self.assertEqual((r["flow"], r["events"]), ("SP-REQ-001", ["parakh.verdict"]))
        self.assertEqual(self.check("C13").status, "pass")

    def test_comments_without_a_footer_and_on_plain_issues_are_ignored(self):
        c = {"action": "created", "repository": {"full_name": "iksula/siteone-pimcore"}, "issue": {"number": 7, "pull_request": {}},
             "comment": {"user": {"login": "x"}, "body": "looks fine to me"}}
        self.assertFalse(self.gh("issue_comment", c)["handled"])
        c["issue"] = {"number": 7}
        self.assertFalse(self.gh("issue_comment", c)["handled"])

    def test_reviews_and_a_merge_by_the_tech_lead_with_a_senior_approval(self):
        self.gh("pull_request", pr(), "d1")
        self.gh("pull_request_review", review("approved", "samr"), "d2")
        self.gh("pull_request_review", review("changes_requested", "dev3"), "d3")
        r = self.gh("pull_request", pr(action="closed", merged=True, merged_by="arjunk"), "d4")
        self.assertEqual(r["events"], ["pr.merged"])
        m = self.store.events(type="pr.merged")[0]
        self.assertEqual((m.data["approvers"], m.data["merger_role"], m.data["senior_approved"]), (["samr"], "tech_lead", True))
        self.assertEqual((self.check("C14").status, self.check("C15").status), ("pass", "pass"))

    def test_a_self_approval_is_caught(self):
        self.gh("pull_request", pr(), "d1")
        self.gh("pull_request_review", review("approved", "dev1"), "d2")
        self.gh("pull_request", pr(action="closed", merged=True, merged_by="dev1"), "d3")
        self.assertEqual(self.check("C14").status, "fail")
        self.assertEqual(self.check("C15").status, "fail")           # and the developer merged it

    def test_an_approval_of_an_older_commit_does_not_count_after_new_pushes(self):
        self.gh("pull_request", pr(), "d1")
        self.gh("pull_request_review", review("approved", "samr", sha=SHA1), "d2")
        self.gh("pull_request", pr(action="synchronize", sha=SHA2), "d3")
        self.gh("pull_request", pr(action="closed", sha=SHA2, merged=True, merged_by="arjunk"), "d4")
        self.assertEqual(self.store.events(type="pr.merged")[0].data["approvers"], [])
        self.assertEqual(self.check("C14").status, "fail")

    def test_a_reviewer_who_changes_their_mind_counts_by_their_latest_state(self):
        self.gh("pull_request", pr(), "d1")
        self.gh("pull_request_review", review("approved", "samr"), "d2")
        self.gh("pull_request_review", review("changes_requested", "samr"), "d3")
        self.gh("pull_request", pr(action="closed", merged=True, merged_by="arjunk"), "d4")
        self.assertEqual(self.store.events(type="pr.merged")[0].data["approvers"], [])

    def test_a_closed_unmerged_pr_other_repos_and_unused_events_are_ignored(self):
        self.assertFalse(self.gh("pull_request", pr(action="closed", merged=False))["handled"])
        self.assertIn("not this project", self.gh("pull_request", pr(repo="someone/else"))["note"])
        self.assertFalse(self.gh("push", {"repository": {"full_name": "iksula/siteone-pimcore"}})["handled"])
        self.assertFalse(self.gh("pull_request_review", {**review(), "action": "edited"})["handled"])
        self.assertEqual(self.types(), ["call.recorded", "pulse.mapped", "tier.set"])

    def test_a_developer_cannot_lower_the_tier_a_lead_set(self):
        self.store.append("tier.set", "SP-REQ-001", "SP", "a", {"tier": "T1", "actor_role": "tech_lead"})
        self.gh("pull_request", pr(body="Ticket: SP-2\nTier: T3\nAI-assisted: yes"), "d1")
        self.assertEqual(self.check("C17").status, "fail")           # still treated as T1: no failure-path test or rollback note
        self.assertEqual(self.check("C13").status, "fail")           # and Parakh is still required

    def test_with_no_lead_tier_the_pr_declaration_is_used(self):
        s2 = open_store(Path(self.dir.name) / "other.jsonl")
        s2.append("pr.opened", "F", "SP", "a", {"pr": 1, "author": "d", "head_sha": "x", "tier": "T3"})
        r = evaluate_store(s2, RULES, flow="F", now=datetime.now(timezone.utc) + timedelta(days=1))[0]
        self.assertEqual(next(c.status for c in r.checks if c.id == "C13"), "na")     # T3: Parakh optional


class ServerTests(Base):
    def ctx(self, **kw):
        base = dict(projects_dir=Path(self.dir.name) / "projects", data_dir=Path(self.dir.name) / "data", rules=RULES,
                    github_secret="gh-secret", slack_secret="sl-secret")
        base.update(kw)
        return Context(**base)

    @staticmethod
    def sign(body, secret="gh-secret"):
        return {"x-hub-signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
                "x-github-event": "pull_request", "x-github-delivery": "srv-1"}

    def post_gh(self, payload, ctx=None, event="pull_request", **hdr):
        body = json.dumps(payload).encode()
        h = {**self.sign(body), "x-github-event": event, **hdr}
        return handle(ctx or self.ctx(), "POST", "/webhooks/github", h, body)

    def test_health_and_unknown_routes(self):
        s, _, b = handle(self.ctx(), "GET", "/health", {})
        self.assertEqual((s, json.loads(b)["projects"]), (200, ["SP"]))
        self.assertEqual(handle(self.ctx(), "GET", "/nope", {})[0], 404)
        self.assertEqual(handle(self.ctx(), "DELETE", "/health", {})[0], 405)

    def test_github_webhooks_are_refused_until_a_secret_is_configured(self):
        s, _, b = self.post_gh(pr(), ctx=self.ctx(github_secret=""))
        self.assertEqual(s, 503)
        self.assertEqual(self.types().count("pr.opened"), 0)

    def test_a_bad_or_missing_signature_is_refused_and_logs_nothing(self):
        body = json.dumps(pr()).encode()
        self.assertEqual(handle(self.ctx(), "POST", "/webhooks/github", {"x-github-event": "pull_request"}, body)[0], 401)
        self.assertEqual(handle(self.ctx(), "POST", "/webhooks/github", {**self.sign(body, "wrong"), "x-github-event": "pull_request"}, body)[0], 401)
        self.assertEqual(self.types().count("pr.opened"), 0)

    def test_a_signed_pr_webhook_finds_the_project_by_repo_and_logs_events(self):
        s, _, b = self.post_gh(pr())
        self.assertEqual(s, 200, b)
        self.assertEqual(json.loads(b)["flow"], "SP-REQ-001")
        self.assertIn("pr.opened", self.types())

    def test_ping_unknown_repo_bad_json_and_oversize(self):
        self.assertEqual(json.loads(self.post_gh({"zen": "x"}, event="ping")[2]), {"pong": True})
        self.assertEqual(self.post_gh(pr(repo="no/such"))[0], 404)
        body = b"{not json"
        self.assertEqual(handle(self.ctx(), "POST", "/webhooks/github", self.sign(body), body)[0], 400)
        big = b"x" * 2000
        self.assertEqual(handle(self.ctx(max_body=1000), "POST", "/webhooks/github", self.sign(big), big)[0], 413)

    def slack(self, payload, ts=None, secret="sl-secret", clock=None):
        body = json.dumps(payload).encode()
        ts = str(ts or int(time.time()))
        sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        return handle(self.ctx(clock=clock), "POST", "/webhooks/slack", {"x-slack-request-timestamp": ts, "x-slack-signature": sig}, body)

    def test_slack_handshake_replay_and_the_documented_stub(self):
        s, _, b = self.slack({"type": "url_verification", "challenge": "abc123"})
        self.assertEqual((s, json.loads(b)["challenge"]), (200, "abc123"))
        self.assertEqual(self.slack({"type": "url_verification", "challenge": "x"}, ts=int(time.time()) - 3600)[0], 401)
        self.assertEqual(self.slack({"type": "url_verification", "challenge": "x"}, secret="wrong")[0], 401)
        s, _, b = self.slack({"type": "event_callback", "event": {"type": "reaction_added", "user": "U1"}})
        self.assertEqual((s, json.loads(b)["stub"]), (200, True))
        raw = (Path(self.dir.name) / "data" / "_slack_events.jsonl").read_text()
        self.assertIn("reaction_added", raw)
        self.assertEqual(handle(self.ctx(slack_secret=""), "POST", "/webhooks/slack", {}, b"{}")[0], 503)

    def test_read_endpoints_need_the_token_when_one_is_set(self):
        c = self.ctx(api_token="t0ken")
        self.assertEqual(handle(c, "GET", "/api/next?project=SP", {})[0], 401)
        self.assertEqual(handle(c, "GET", "/api/next?project=SP", {"Authorization": "Bearer nope"})[0], 401)
        s, _, b = handle(c, "GET", "/api/next?project=SP", {"Authorization": "Bearer t0ken"})
        self.assertEqual(s, 200)
        self.assertIn("orders", json.loads(b))

    def test_project_keys_are_validated_before_touching_the_file_system(self):
        for bad in ("../../etc/passwd", "sp/../x", "", "lower case", "A" * 40):
            s, _, b = handle(self.ctx(), "GET", "/api/next?project=" + bad, {})
            self.assertEqual(s, 400, bad)
        self.assertEqual(handle(self.ctx(), "GET", "/api/next?project=ZZ", {})[0], 404)

    def test_the_http_wrapper_round_trips_and_refuses_an_open_non_local_bind(self):
        srv = make_server(self.ctx(), "127.0.0.1", 0)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            port = srv.server_address[1]
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
            self.assertEqual((r.status, r.headers["X-Content-Type-Options"], json.loads(r.read())["ok"]), (200, "nosniff", True))
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()
        with self.assertRaises(SystemExit):
            make_server(self.ctx(), "0.0.0.0", 0)
        make_server(self.ctx(api_token="x"), "0.0.0.0", 0).server_close()


if __name__ == "__main__":
    unittest.main()
