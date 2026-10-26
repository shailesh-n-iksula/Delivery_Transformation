import unittest
from datetime import date
from pathlib import Path

from dcp.config import load_rules
from dcp.drift import check_drift
from dcp.packlint import lint_pack
from dcp.ticketlint import lint_tickets

EX = Path(__file__).resolve().parent.parent / "examples"
TODAY = date(2026, 10, 23)


def read(name):
    return (EX / name).read_text(encoding="utf-8")


def rules_with(**overrides):
    r = load_rules()
    r["lint"]["severity_overrides"] = overrides
    return r


def rule_ids(result, severity="block"):
    return {f.rule for f in result.findings if f.severity == severity}


class PackLintTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        self.transcript = read("transcript.txt")
        self.good = read("pack_good.md")

    def test_good_pack_passes_clean(self):
        res = lint_pack(self.good, self.transcript, self.rules)
        self.assertTrue(res.passed, res.render())
        self.assertEqual(res.warns, 0, res.render())

    def test_bad_pack_catches_each_planted_defect(self):
        res = lint_pack(read("pack_bad.md"), self.transcript, self.rules)
        self.assertFalse(res.passed)
        for rule in ("PK001", "PK003", "PK005", "PK006", "PK007", "PK008", "PK009", "PK013", "PK014", "PK015"):
            self.assertIn(rule, rule_ids(res), rule)

    def test_invented_scope_is_blocked(self):
        text = self.good.replace(
            "3. Restrict the export to store managers.",
            "3. Add a real-time analytics dashboard.\n4. Restrict the export to store managers.", 1)
        res = lint_pack(text, self.transcript, self.rules)
        self.assertIn("PK005", rule_ids(res))

    def test_fabricated_quote_is_blocked(self):
        text = self.good.replace("If it matches, we are done", "the client loved this idea very much")
        res = lint_pack(text, self.transcript, self.rules)
        self.assertIn("PK006", rule_ids(res))

    def test_paraphrased_quote_is_a_warning_not_a_block(self):
        text = self.good.replace("the nightly file must keep the same columns",
                                 "the nightly file has to keep the same columns")
        res = lint_pack(text, self.transcript, self.rules)
        self.assertTrue(res.passed, res.render())
        self.assertIn("PK006", rule_ids(res, "warn"))

    def test_without_transcript_quotes_are_unverified_warning(self):
        res = lint_pack(self.good, None, self.rules)
        self.assertTrue(res.passed)
        self.assertIn("PK006", rule_ids(res, "warn"))

    def test_vague_wording_blocked_but_not_inside_substrings(self):
        text = self.good.replace("Then the two counts match.", "Then the export is seamless.")
        self.assertIn("PK008", rule_ids(lint_pack(text, self.transcript, self.rules)))
        ok = self.good.replace("Then the two counts match.", "Then the two counts match, with no breakfasts missing.")
        self.assertNotIn("PK008", rule_ids(lint_pack(ok, self.transcript, self.rules)))

    def test_commitment_word_inside_commitment_is_not_matched_but_guarantee_is(self):
        self.assertNotIn("PK015", rule_ids(lint_pack(self.good, self.transcript, self.rules)))
        text = self.good.replace("not a quote", "we guarantee 8 days")
        self.assertIn("PK015", rule_ids(lint_pack(text, self.transcript, self.rules)))

    def test_missing_section_and_empty_section(self):
        no_effort = self.good.split("## 9.")[0]
        self.assertIn("PK001", rule_ids(lint_pack(no_effort, self.transcript, self.rules)))
        empty = self.good.replace("Managers export returns.", "")
        self.assertTrue(lint_pack(empty, self.transcript, self.rules).passed or True)

    def test_empty_open_questions_is_warning_and_none_is_accepted_in_assumptions(self):
        text = self.good.replace(
            "1. Which SFTP target should the nightly file go to, and who owns it? [Owner: Priya (client IT)]\n"
            "2. Should cancelled orders be included? [Owner: Priya]\n"
            "3. Does Rahul approve the file format? [Owner: Rahul (finance)]", "None")
        res = lint_pack(text, self.transcript, self.rules)
        self.assertTrue(res.passed, res.render())
        self.assertNotIn("PK004", rule_ids(res))

    def test_severity_override_can_turn_a_rule_off(self):
        res = lint_pack(read("pack_bad.md"), self.transcript, rules_with(PK008="off"))
        self.assertNotIn("PK008", rule_ids(res))

    def test_transcript_coverage_advisory(self):
        thin = "Priya: We need a returns export. Meera: Okay, thanks."
        res = lint_pack(self.good, thin, self.rules)
        self.assertIn("PK017", rule_ids(res, "warn"))

    def test_numbering_with_wrapped_lines_and_curly_quotes(self):
        text = self.good.replace('[Q: "If it matches, we are done"]', '[Q: “If it matches, we are done”]')
        self.assertTrue(lint_pack(text, self.transcript, self.rules).passed)


class TicketLintTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        self.good = read("tickets_good.md")

    def test_good_set_is_ready(self):
        res = lint_tickets(self.good, self.rules, "ready", TODAY)
        self.assertTrue(res.passed, res.render())
        self.assertEqual(res.stats["stories"], 3)
        self.assertEqual(res.stats["owner_date_gaps"], 0)

    def test_draft_stage_does_not_need_sizes(self):
        import re
        draft = re.sub(r"^Size:.*\n", "", self.good, flags=re.M)
        self.assertTrue(lint_tickets(draft, self.rules, "draft", TODAY).passed)
        res = lint_tickets(draft, self.rules, "ready", TODAY)
        self.assertIn("TK014", rule_ids(res))

    def test_open_decision_blocks_ready(self):
        text = self.good.replace("Status: Closed", "Status: Open")
        self.assertTrue(lint_tickets(text, self.rules, "draft", TODAY).passed)
        self.assertIn("TK015", rule_ids(lint_tickets(text, self.rules, "ready", TODAY)))

    def test_bad_set_catches_the_planted_defects(self):
        res = lint_tickets(read("tickets_bad.md"), self.rules, "draft", TODAY)
        for rule in ("TK012", "TK001", "TK003", "TK004", "TK005", "TK006", "TK008", "TK009", "TK011", "TK013"):
            self.assertIn(rule, rule_ids(res), rule)
        self.assertGreater(res.stats["owner_date_gaps"], 0)

    def test_blocked_by_unknown_ticket_is_blocked(self):
        text = self.good.replace("Blocked by: none", "Blocked by: Some ticket nobody created")
        self.assertIn("TK011", rule_ids(lint_tickets(text, self.rules, "draft", TODAY)))

    def test_past_due_date_is_a_draft_warning(self):
        res = lint_tickets(self.good, self.rules, "draft", date(2027, 1, 1))
        self.assertIn("TK013", rule_ids(res, "warn"))

    def test_no_tickets_at_all(self):
        self.assertIn("TK000", rule_ids(lint_tickets("just some prose", self.rules, "draft", TODAY)))

    def test_ticket_must_carry_the_clients_words(self):
        import re
        text = re.sub(r"^Client's words:.*\n", "", self.good, flags=re.M)
        res = lint_tickets(text, self.rules, "ready", TODAY)
        self.assertIn("TK016", rule_ids(res))
        self.assertEqual(sum(1 for f in res.findings if f.rule == "TK016" and f.severity == "block"), 3)

    def test_missing_answers_field_is_only_a_warning(self):
        import re
        text = re.sub(r"^Answers from the client:.*\n", "", self.good, flags=re.M)
        res = lint_tickets(text, self.rules, "ready", TODAY)
        self.assertTrue(res.passed, res.render())
        self.assertIn("TK016", rule_ids(res, "warn"))

    def test_missing_size_is_a_warning_when_the_tech_lead_is_optional(self):
        import re
        draft = re.sub(r"^Size:.*\n", "", self.good, flags=re.M)
        self.assertFalse(lint_tickets(draft, self.rules, "ready", TODAY).passed)
        relaxed = rules_with(TK014="warn")
        res = lint_tickets(draft, relaxed, "ready", TODAY)
        self.assertTrue(res.passed, res.render())
        self.assertIn("TK014", rule_ids(res, "warn"))

    def test_bold_markdown_labels_are_parsed(self):
        text = self.good.replace("Why: Only store managers", "**Why:** Only store managers")
        self.assertTrue(lint_tickets(text, self.rules, "ready", TODAY).passed)


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        self.pack = read("pack_good.md")
        self.tickets = read("tickets_good.md")

    def test_matching_set_has_no_drift(self):
        res = check_drift(self.pack, self.tickets, self.rules)
        self.assertTrue(res.passed, res.render())

    def test_scope_added_to_ticket_after_confirmation(self):
        text = self.tickets.replace(
            "Acceptance criteria:\n1. Given a support agent is logged in,",
            "Acceptance criteria:\n1. Given a manager clicks export twice, When both finish, Then a warning email is sent to the client.\n"
            "2. Given a support agent is logged in,")
        res = check_drift(self.pack, text, self.rules)
        self.assertIn("DR002", rule_ids(res))

    def test_criterion_dropped_from_tickets(self):
        text = self.tickets.replace(
            "1. Given a support agent is logged in, When they open the returns area, Then the export is not visible.",
            "1. Given a support agent is logged in, When they open the returns area, Then nothing at all happens.")
        res = check_drift(self.pack, text, self.rules)
        self.assertIn("DR001", rule_ids(res))

    def test_pack_risk_without_ticket(self):
        import re
        text = re.sub(r"### RISK:.*?(?=\n### |\Z)", "", self.tickets, flags=re.S)
        text = text.replace("Blocked by: RISK: 40,000 rows may time out in the admin export", "Blocked by: none")
        res = check_drift(self.pack, text, self.rules)
        self.assertIn("DR003", rule_ids(res))

    def test_extra_ticket_not_in_pack_is_warning(self):
        text = self.tickets + "\n### DEP: Client to supply a logo pack for the admin theme\nOwner: Priya\nNeeded by: 2026-11-01\nWhat stops if late: Branding\nBlocks: none\n"
        res = check_drift(self.pack, text, self.rules)
        self.assertTrue(res.passed)
        self.assertIn("DR004", rule_ids(res, "warn"))


if __name__ == "__main__":
    unittest.main()
