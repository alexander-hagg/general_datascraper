"""Tests for identity, extraction modes, row indexes and validation."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook, load_workbook

import main
import searchers


def make_search_workbook(path, rows):
    header = [
        "source", "title", "authors", "doi", "pmid", "pmcid",
        "year", "journal", "is_open_access", "fulltext_url",
    ]
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)


def read_sheet(path):
    wb = load_workbook(path, read_only=True)
    ws = wb.active
    rows = [list(row) for row in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


class SearchIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_search_results_derives_missing_paper_key(self):
        path = self.dir / "search_results.xlsx"
        make_search_workbook(path, [
            ["pubmed", "PMID paper", "A", "10.1/PMID", "111", "PMC1", "2020", "J", "yes", "u"],
            ["crossref", "DOI paper", "B", "10.1/AbC", "", "", "2021", "J", "", ""],
            ["europepmc", "Some Paper", "C", "", "", "", "2022", "J", "", ""],
        ])

        with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", path):
            papers = main._load_search_results()

        self.assertEqual([p["paper_key"] for p in papers], [
            "pmid:111",
            "doi:10.1/abc",
            "title:some paper",
        ])
        self.assertEqual([p["title"] for p in papers], ["PMID paper", "DOI paper", "Some Paper"])
        self.assertTrue(papers[0]["is_oa"])
        self.assertFalse(papers[1]["is_oa"])

    def test_load_search_results_keeps_stored_paper_key(self):
        path = self.dir / "search_results.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(["paper_key", "title", "doi", "pmid"])
        ws.append(["doi:stable-key", "A title", "10.1/x", "111"])
        wb.save(path)

        with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", path):
            papers = main._load_search_results()

        self.assertEqual(papers[0]["paper_key"], "doi:stable-key")

    def test_paper_to_row_contains_paper_key(self):
        row = main.paper_to_row({
            "sources": ["pubmed"],
            "title": "A",
            "doi": "10.1/X",
            "pmid": "42",
            "is_oa": True,
        })
        self.assertEqual(row["paper_key"], "pmid:42")
        self.assertEqual(row["source"], "pubmed")
        self.assertTrue(main.paper_to_row({"title": "Only title"})["paper_key"].startswith("title:"))

    def test_run_search_writes_paper_key_column(self):
        path = self.dir / "search_results.xlsx"
        paper = {
            "sources": ["pubmed"],
            "title": "Paper",
            "authors": "A",
            "doi": "10.1/X",
            "pmid": "42",
            "pmcid": "",
            "year": "2020",
            "journal": "J",
            "is_oa": True,
            "fulltext_url": "",
        }
        with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", path), \
             mock.patch.object(
                 main.searchers, "search_all",
                 lambda limit=None, year_from=None, year_to=None: [paper],
             ), \
             mock.patch.object(main, "_load_search_results", lambda required=False: []):
            main.run_search()

        rows = read_sheet(path)
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual(row["paper_key"], "pmid:42")
        self.assertEqual(row["title"], "Paper")


class JournalFilterSearchTests(unittest.TestCase):
    def test_run_search_filters_merged_rows_and_reports_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            search = Path(tmp) / "search_results.xlsx"
            # Existing workbook: one row that the new blocklist now removes and
            # one that must survive the merge.
            make_search_workbook(search, [
                ["pubmed", "Old blocked", "A", "10.1/old-blocked", "", "", "2020",
                 "Predatory Journal of Examples", "yes", ""],
                ["pubmed", "Old kept", "B", "10.1/old-kept", "", "", "2020",
                 "Good Journal", "yes", ""],
            ])
            new_papers = [
                {"sources": ["crossref"], "title": "New blocked", "doi": "10.1/new-blocked",
                 "year": "2021", "journal": "PREDATORY JOURNAL OF EXAMPLES"},
                {"sources": ["crossref"], "title": "New kept", "doi": "10.1/new-kept",
                 "year": "2021", "journal": "Good Journal"},
                {"sources": ["crossref"], "title": "No journal", "doi": "10.1/no-journal",
                 "year": "2021", "journal": ""},
            ]

            output = io.StringIO()
            with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", search), \
                 mock.patch.object(main.config, "EXCLUDED_JOURNALS",
                                   ["predatory journal of examples"]), \
                 mock.patch.object(
                     main.searchers, "search_all",
                     lambda limit=None, year_from=None, year_to=None: new_papers,
                 ), \
                 contextlib.redirect_stdout(output):
                papers = main.run_search()

            self.assertEqual(
                [p["title"] for p in papers],
                ["Old kept", "New kept", "No journal"],
            )
            rows = read_sheet(search)
            header = rows[0]
            titles = [dict(zip(header, row))["title"] for row in rows[1:]]
            self.assertEqual(titles, ["Old kept", "New kept", "No journal"])
            self.assertIn("Excluded 2 paper(s) by journal blocklist.", output.getvalue())

    def test_run_search_without_blocklist_keeps_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            search = Path(tmp) / "search_results.xlsx"
            paper = {
                "sources": ["pubmed"],
                "title": "Paper",
                "doi": "10.1/X",
                "journal": "Any Journal",
                "is_oa": True,
            }
            output = io.StringIO()
            with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", search), \
                 mock.patch.object(main.config, "EXCLUDED_JOURNALS", []), \
                 mock.patch.object(
                     main.searchers, "search_all",
                     lambda limit=None, year_from=None, year_to=None: [paper],
                 ), \
                 mock.patch.object(main, "_load_search_results", lambda required=False: []), \
                 contextlib.redirect_stdout(output):
                papers = main.run_search()

            self.assertEqual([p["title"] for p in papers], ["Paper"])
            self.assertIn("Excluded 0 paper(s) by journal blocklist.", output.getvalue())


class ExtractRowTests(unittest.TestCase):
    def test_row_index_restarts_for_each_paper(self):
        records = [
            {"subunit": "a4", "effect": "GOF"},
            {"subunit": "b2", "effect": "LOF"},
            {"subunit": "g2", "effect": "GOF"},
        ]
        paper_one = {"title": "One", "doi": "10.1/one", "pmid": "1", "sources": ["pubmed"]}
        paper_two = {"title": "Two", "doi": "", "pmid": "", "sources": ["crossref"]}

        rows_one = main.build_extract_rows(
            paper_one, records, ["subunit", "effect"], pdf_file="PMID_1.pdf", pdf_source="article:pmc"
        )
        rows_two = main.build_extract_rows(
            paper_two, records, ["subunit", "effect"], pdf_file="", pdf_source="none"
        )

        self.assertEqual([r["row_index"] for r in rows_one], [1, 2, 3])
        self.assertEqual([r["row_index"] for r in rows_two], [1, 2, 3])
        self.assertEqual({r["paper_key"] for r in rows_one}, {"pmid:1"})
        self.assertEqual({r["paper_key"] for r in rows_two}, {"title:two"})
        self.assertEqual(rows_one[0]["subunit"], "a4")
        self.assertEqual(rows_one[0]["pdf_file"], "PMID_1.pdf")
        self.assertEqual(rows_one[0]["pdf_source"], "article:pmc")
        self.assertEqual(rows_one[0]["evidence"], "")
        self.assertEqual(rows_one[0]["evidence_page"], "")

    def test_non_dict_records_are_safe(self):
        paper = {"title": "One", "doi": "10.1/one", "sources": ["pubmed"]}
        rows = main.build_extract_rows(paper, ["not a dict"], ["subunit"])
        self.assertEqual(rows[0]["subunit"], "")
        self.assertEqual(rows[0]["row_index"], 1)

    def test_rows_copy_the_verbatim_evidence_field(self):
        paper = {"title": "One", "doi": "10.1/one", "sources": ["pubmed"]}
        records = [
            {"subunit": "a4", "evidence": "A verbatim quote from the paper."},
            {"subunit": "b2"},
            {"subunit": "g2", "evidence": None},
        ]
        rows = main.build_extract_rows(paper, records, ["subunit"])
        self.assertEqual(rows[0]["evidence"], "A verbatim quote from the paper.")
        self.assertEqual(rows[1]["evidence"], "")
        self.assertEqual(rows[2]["evidence"], "")
        self.assertEqual([r["evidence_page"] for r in rows], ["", "", ""])

    def test_run_extract_uses_mode_and_writes_per_paper_row_indexes(self):
        records = [{"subunit": "a4"}, {"subunit": "b2"}]
        seen = {}

        def fake_extract(text, fields=None, instruction=None):
            seen["instruction"] = instruction
            return list(records)

        papers = [
            {"title": "Paper One", "doi": "10.1/one", "pmid": "111", "sources": ["pubmed"]},
            {"title": "Paper Two", "doi": "", "pmid": "", "sources": ["crossref"]},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            extracted = tmp / "extracted_data.xlsx"
            closed = tmp / "closed.xlsx"
            checkpoint = tmp / "checkpoint.json"
            with mock.patch.object(main.config, "EXTRACTED_XLSX", extracted), \
                 mock.patch.object(main.config, "CLOSED_SOURCE_XLSX", closed), \
                 mock.patch.object(main.config, "CHECKPOINT_JSON", checkpoint), \
                 mock.patch.object(main.config, "PDF_ARCHIVE_ENABLED", False), \
                 mock.patch.object(main.extractor, "check_backend", lambda: None), \
                 mock.patch.object(
                     main,
                     "_prepare_paper",
                     lambda p: ("full text " * 100, "test", {
                         "path": None, "filename": "", "label": "none", "reason": "no PDF",
                     }),
                 ), \
                 mock.patch.object(main.extractor, "extract", fake_extract):
                main.run_extract(papers=papers, mode="table")

            self.assertIn("TABLE ROWS", seen["instruction"])
            self.assertIn("NEVER merge", seen["instruction"])
            rows = read_sheet(extracted)
            header = rows[0]
            objects = [dict(zip(header, row)) for row in rows[1:]]
            self.assertEqual([o["row_index"] for o in objects], [1, 2, 1, 2])
            self.assertEqual(objects[0]["paper_key"], "pmid:111")
            self.assertEqual(objects[2]["paper_key"], "title:paper two")
            self.assertEqual(objects[0]["subunit"], "a4")
            self.assertEqual(objects[3]["subunit"], "b2")
            for name in ("paper_key", "row_index", "evidence", "evidence_page", "pdf_file", "pdf_source"):
                self.assertIn(name, header)


class InstructionAndModeTests(unittest.TestCase):
    def test_records_and_table_wording_differ(self):
        records = main.make_instruction("Extract mutation data", mode="records")
        table = main.make_instruction("Extract mutation data", mode="table")

        self.assertIn("INDIVIDUAL RECORDS", records)
        self.assertIn("EVERY record", records)
        self.assertNotIn("TABLE ROWS", records)

        self.assertIn("TABLE ROWS", table)
        self.assertIn("ONE JSON object per row", table)
        self.assertIn("NEVER merge", table)
        self.assertIn("Extract mutation data", table)

    def test_goal_default_and_extra_instruction(self):
        text = main.make_instruction(mode="records", extra="Keep animal strains.")
        self.assertIn("Extract the relevant data described in the text.", text)
        self.assertIn("ADDITIONAL INSTRUCTIONS:", text)
        self.assertIn("Keep animal strains.", text)

    def test_mode_validation(self):
        self.assertEqual(main.normalize_extraction_mode(None), "records")
        self.assertEqual(main.normalize_extraction_mode("TABLE"), "table")
        with self.assertRaises(ValueError):
            main.normalize_extraction_mode("figures")

    def test_extract_columns_include_reserved_placeholders(self):
        columns = main.extract_columns(["mutation"])
        self.assertEqual(columns, [
            "paper_key", "row_index", "doi", "source", "publication_type",
            "pdf_file", "pdf_source", "evidence", "evidence_page", "mutation",
        ])
        self.assertIn("paper_key", main.SEARCH_COLS)
        self.assertIn("publication_type", main.SEARCH_COLS)


class ColumnValidationTests(unittest.TestCase):
    def test_reserves_and_duplicates_are_rejected(self):
        cleaned, errors = main.validate_user_columns([
            "mutation", "paper_key", "Evidence", "doi", "mutation", "", None,
        ])
        self.assertEqual(cleaned, ["mutation"])
        joined = " ".join(errors)
        self.assertIn("paper_key", joined)
        self.assertIn("Evidence", joined)
        self.assertIn("doi", joined)
        self.assertIn("Duplicate column names", joined)
        self.assertIn("mutation", joined)

    def test_empty_columns_are_allowed_and_fall_back(self):
        self.assertEqual(main.validate_user_columns([]), ([], []))
        self.assertEqual(main.validate_user_columns(None), ([], []))
        cleaned, errors = main.validate_user_columns("a, b, a")
        self.assertEqual(cleaned, ["a", "b"])
        self.assertTrue(errors)

    def test_extract_user_columns_filters_technical_names(self):
        header = [
            "paper_key", "row_index", "doi", "source", "pdf_file", "pdf_source",
            "evidence", "evidence_page", "subunit", "effect", None, "",
        ]
        self.assertEqual(main.extract_user_columns(header), ["subunit", "effect"])


class EvidenceInstructionAndExtractorTests(unittest.TestCase):
    def test_instruction_adds_the_evidence_rule(self):
        records = main.make_instruction("Extract mutations", mode="records")
        table = main.make_instruction("Extract mutations", mode="table")
        for text in (records, table):
            self.assertIn('"evidence"', text)
            self.assertIn("VERBATIM", text)
            self.assertIn("300", text)
            self.assertIn("NEVER invent", text)
        self.assertIn("TABLE ROW", table)
        self.assertNotIn("TABLE ROW", records)

    def test_instruction_omits_evidence_when_disabled(self):
        with mock.patch.object(main.config, "EVIDENCE_ENABLED", False):
            text = main.make_instruction("Extract mutations", mode="records")
        self.assertNotIn('"evidence"', text)
        self.assertIn("INDIVIDUAL RECORDS", text)

    def test_schema_and_prompt_include_evidence(self):
        schema = main.extractor.build_schema([("mutation", "text")])
        item = schema["items"]
        self.assertEqual(item["properties"]["evidence"], {"type": "string"})
        self.assertIn("evidence", item["required"])

        with mock.patch.object(main.extractor.config, "LLM_BACKEND", "ollama"):
            ollama = main.extractor._build_prompt(
                "TEXT", "Extract mutations", [("mutation", "text")]
            )
        self.assertIn('"evidence"', ollama)
        self.assertIn("VERBATIM", ollama)
        self.assertIn("JSON array", ollama)

        with mock.patch.object(main.extractor.config, "LLM_BACKEND", "openai"):
            openai = main.extractor._build_prompt(
                "TEXT", "Extract mutations", [("mutation", "text")]
            )
        self.assertIn('"items"', openai)
        self.assertIn('"evidence"', openai)

    def test_schema_and_prompt_drop_evidence_when_disabled(self):
        with mock.patch.object(main.extractor.config, "EVIDENCE_ENABLED", False):
            schema = main.extractor.build_schema([("mutation", "text")])
            prompt = main.extractor._build_prompt(
                "TEXT", "Extract mutations", [("mutation", "text")]
            )
        self.assertNotIn("evidence", schema["items"]["properties"])
        self.assertNotIn('"evidence"', prompt)

    def test_extract_guarantees_a_string_evidence_field(self):
        with mock.patch.object(main.extractor.config, "LLM_BACKEND", "ollama"), \
             mock.patch.object(
                 main.extractor, "_call_ollama", lambda prompt, fields: '[{"mutation": "M1"}]'
             ):
            records = main.extractor.extract("TEXT", fields=[("mutation", "text")])
        self.assertEqual(records, [{"mutation": "M1", "evidence": ""}])

        with mock.patch.object(main.extractor.config, "EVIDENCE_ENABLED", False), \
             mock.patch.object(main.extractor.config, "LLM_BACKEND", "ollama"), \
             mock.patch.object(
                 main.extractor, "_call_ollama", lambda prompt, fields: '[{"mutation": "M1"}]'
             ):
            records = main.extractor.extract("TEXT", fields=[("mutation", "text")])
        self.assertEqual(records, [{"mutation": "M1"}])


class RunExtractEvidenceTests(unittest.TestCase):
    def _paths(self, tmp):
        return (
            tmp / "extracted_data.xlsx",
            tmp / "closed.xlsx",
            tmp / "checkpoint.json",
        )

    def _paper(self):
        return {
            "title": "Paper One",
            "doi": "10.1/one",
            "pmid": "111",
            "sources": ["pubmed"],
        }

    def _run(self, annotate, pdf_path):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            extracted, closed, checkpoint = self._paths(tmp)
            fig_dir = tmp / "figures"
            with mock.patch.object(main.config, "EXTRACTED_XLSX", extracted), \
                 mock.patch.object(main.config, "CLOSED_SOURCE_XLSX", closed), \
                 mock.patch.object(main.config, "CHECKPOINT_JSON", checkpoint), \
                 mock.patch.object(main.config, "FIGURES_DIR", fig_dir), \
                 mock.patch.object(main.config, "FIGURES_INDEX_JSON", fig_dir / "index.json"), \
                 mock.patch.object(main.config, "FIGURES_XLSX", tmp / "figures.xlsx"), \
                 mock.patch.object(main.extractor, "check_backend", lambda: None), \
                 mock.patch.object(
                     main,
                     "_prepare_paper",
                     lambda p: ("full text " * 100, "test", {
                         "path": pdf_path,
                         "filename": "PMID_111.pdf" if pdf_path else "",
                         "label": "article:pmc" if pdf_path else "none",
                         "reason": "",
                     }),
                 ), \
                 mock.patch.object(
                     main.extractor,
                     "extract",
                     lambda text, fields=None, instruction=None:
                         [{"subunit": "a4", "evidence": "mutation increased current"}],
                 ), \
                 mock.patch.object(
                     main.pdf_annotations, "annotate_paper_rows", side_effect=annotate
                 ):
                main.run_extract(papers=[self._paper()], mode="records")
            return read_sheet(extracted)

    def test_run_extract_updates_evidence_page_before_writing(self):
        def fake(rows, path, **kwargs):
            rows[0]["evidence_page"] = "4"
            return rows, {
                "located": 1, "unresolved": 0, "warning": "",
                "evidence": [],
            }

        table = self._run(fake, Path("/tmp/archived.pdf"))
        objects = [dict(zip(table[0], row)) for row in table[1:]]
        self.assertEqual(objects[0]["evidence"], "mutation increased current")
        self.assertEqual(objects[0]["evidence_page"], "4")

    def test_annotation_failure_only_warns(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            table = self._run(RuntimeError("broken PDF"), Path("/tmp/archived.pdf"))
        objects = [dict(zip(table[0], row)) for row in table[1:]]
        self.assertEqual(objects[0]["evidence"], "mutation increased current")
        self.assertIn(objects[0]["evidence_page"], (None, ""))
        self.assertIn("evidence warning: broken PDF", output.getvalue())

    def test_no_pdf_skips_annotation_and_keeps_evidence_text(self):
        annotate = mock.Mock()
        table = self._run(annotate, None)
        annotate.assert_not_called()
        objects = [dict(zip(table[0], row)) for row in table[1:]]
        self.assertEqual(objects[0]["evidence"], "mutation increased current")
        self.assertIn(objects[0]["evidence_page"], (None, ""))


class KeyFunctionTests(unittest.TestCase):
    def test_searchers_paper_key_order(self):
        self.assertEqual(searchers.paper_key({"pmid": "1", "doi": "10.1/X", "title": "T"}), "pmid:1")
        self.assertEqual(searchers.paper_key({"doi": "10.1/X", "title": "T"}), "doi:10.1/x")
        self.assertEqual(searchers.paper_key({"title": " Some Title "}), "title:some title")


if __name__ == "__main__":
    unittest.main()
