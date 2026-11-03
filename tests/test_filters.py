"""Tests for the post-search journal blocklist (filters.py)."""

import unittest

import filters


class ParseJournalTermsTests(unittest.TestCase):
    def test_none_and_blank_values(self):
        self.assertEqual(filters.parse_journal_terms(None), [])
        self.assertEqual(filters.parse_journal_terms(""), [])
        self.assertEqual(filters.parse_journal_terms("  \n\n \t \n"), [])
        self.assertEqual(filters.parse_journal_terms([None, "", "  "]), [])

    def test_newline_separated_string(self):
        value = "  Journal One  \n\n journal two \n   \nJournal Three\n"
        self.assertEqual(
            filters.parse_journal_terms(value),
            ["Journal One", "journal two", "Journal Three"],
        )

    def test_list_and_tuple_keep_first_spelling(self):
        self.assertEqual(
            filters.parse_journal_terms(["Journal One", "JOURNAL ONE", "Journal Two"]),
            ["Journal One", "Journal Two"],
        )
        self.assertEqual(
            filters.parse_journal_terms(("Journal One", "Journal Two")),
            ["Journal One", "Journal Two"],
        )

    def test_dedupe_ignores_case_and_internal_spacing(self):
        self.assertEqual(
            filters.parse_journal_terms("Journal   of  Tests\njournal of tests"),
            ["Journal   of  Tests"],
        )


class MatchingTests(unittest.TestCase):
    def test_normalize_journal(self):
        self.assertEqual(
            filters.normalize_journal("  Journal   of\tTests  "),
            "journal of tests",
        )
        self.assertEqual(filters.normalize_journal(None), "")

    def test_case_insensitive_substring(self):
        terms = ["journal of questionable"]
        self.assertTrue(
            filters.journal_matches("The Journal of Questionable Results", terms)
        )
        self.assertTrue(
            filters.journal_matches("JOURNAL OF QUESTIONABLE-RESULTS", terms)
        )
        self.assertFalse(filters.journal_matches("Journal of Good Results", terms))

    def test_punctuation_and_spacing_variants_match(self):
        terms = filters.parse_journal_terms("Journal of  Questionable Results")
        self.assertTrue(
            filters.journal_matches("journal of questionable results.", terms)
        )
        self.assertTrue(
            filters.journal_matches("Journal of  Questionable   Results", terms)
        )

    def test_empty_journal_or_empty_terms_never_match(self):
        self.assertFalse(filters.journal_matches("", ["Journal"]))
        self.assertFalse(filters.journal_matches(None, ["Journal"]))
        self.assertFalse(filters.journal_matches("Journal", []))
        self.assertFalse(filters.journal_matches("Journal", [""]))

    def test_is_excluded_uses_journal_field_only(self):
        self.assertTrue(filters.is_excluded({"journal": "Blocked Journal"}, ["blocked"]))
        self.assertFalse(filters.is_excluded({"journal": ""}, ["blocked"]))
        self.assertFalse(filters.is_excluded({"title": "Blocked Journal"}, ["blocked"]))
        self.assertFalse(filters.is_excluded(None, ["blocked"]))


class FilterPapersTests(unittest.TestCase):
    def test_split_keeps_order_and_does_not_mutate(self):
        papers = [
            {"title": "A", "journal": "Good Journal"},
            {"title": "B", "journal": "Predatory Journal of Examples"},
            {"title": "C"},
            {"title": "D", "journal": "Another Blocked Place"},
        ]
        original = [dict(p) for p in papers]
        kept, excluded = filters.filter_papers(papers, ["predatory", "blocked place"])

        self.assertEqual([p["title"] for p in kept], ["A", "C"])
        self.assertEqual([p["title"] for p in excluded], ["B", "D"])
        self.assertEqual(len(excluded), 2)
        self.assertEqual(papers, original)
        self.assertIsNot(kept, papers)
        self.assertIsNot(excluded, papers)

    def test_no_terms_keeps_everything(self):
        papers = [{"title": "A", "journal": "Anything"}]
        kept, excluded = filters.filter_papers(papers, [])
        self.assertEqual(kept, papers)
        self.assertEqual(excluded, [])

    def test_string_terms_are_accepted_too(self):
        papers = [{"journal": "Journal One"}, {"journal": "Journal Two"}]
        kept, excluded = filters.filter_papers(papers, "journal one")
        self.assertEqual([p["journal"] for p in kept], ["Journal Two"])
        self.assertEqual([p["journal"] for p in excluded], ["Journal One"])


if __name__ == "__main__":
    unittest.main()
