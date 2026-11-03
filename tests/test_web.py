"""Flask API tests: legacy results and extraction payload compatibility."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

import app as web


def make_workbook(path, header, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    wb.save(path)


def read_sheet(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    ws = wb.active
    rows = [list(row) for row in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


class ResultsApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.search = self.dir / "search_results.xlsx"
        self.extracted = self.dir / "extracted_data.xlsx"
        self.closed = self.dir / "closed_source_papers.xlsx"
        self.client = web.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def test_legacy_sheets_get_paper_keys_without_shape_change(self):
        make_workbook(
            self.search,
            [
                "source", "title", "authors", "doi", "pmid", "pmcid",
                "year", "journal", "is_open_access", "fulltext_url",
            ],
            [["pubmed", "Paper", "A", "10.1/X", "7", "", "2020", "J", "yes", ""]],
        )
        make_workbook(
            self.extracted,
            ["doi", "source", "pdf_file", "pdf_source", "mutation"],
            [["10.1/X", "pubmed", "", "none", "M1"]],
        )

        with mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", self.search), \
             mock.patch.object(web.config, "EXTRACTED_XLSX", self.extracted), \
             mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", self.closed):
            response = self.client.get("/api/results")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(set(payload), {"search", "extracted", "closed"})
        self.assertIsNone(payload["closed"])

        search = payload["search"]
        self.assertIn("paper_key", search["header"])
        search_row = dict(zip(search["header"], search["rows"][0]))
        self.assertEqual(search_row["paper_key"], "pmid:7")
        self.assertEqual(search_row["title"], "Paper")

        extracted = payload["extracted"]
        self.assertIn("paper_key", extracted["header"])
        extracted_row = dict(zip(extracted["header"], extracted["rows"][0]))
        self.assertEqual(extracted_row["paper_key"], "doi:10.1/x")
        self.assertEqual(extracted_row["mutation"], "M1")

    def test_legacy_extracted_rows_without_doi_are_not_given_a_title_identity(self):
        make_workbook(
            self.extracted,
            ["doi", "title", "mutation"],
            [["", "A user field called title", "M1"]],
        )

        with mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", self.search), \
             mock.patch.object(web.config, "EXTRACTED_XLSX", self.extracted), \
             mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", self.closed):
            payload = self.client.get("/api/results").get_json()

        extracted = payload["extracted"]
        extracted_row = dict(zip(extracted["header"], extracted["rows"][0]))
        self.assertEqual(extracted_row["paper_key"], "")
        self.assertEqual(extracted_row["title"], "A user field called title")


class ExtractApiTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        self.calls = []

    def _fake_start(self, job_type, fn, *args):
        self.calls.append((job_type, fn, args))
        return {"id": "job123"}, None

    def _post(self, payload):
        with mock.patch.object(web, "_start_job", new=self._fake_start):
            return self.client.post("/api/extract", json=payload)

    def test_mode_defaults_to_records_for_old_clients(self):
        response = self._post({"goal": "Extract mutations", "columns": ["mutation"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mode"], "records")
        self.assertEqual(self.calls[0][0], "extract")
        self.assertEqual(self.calls[0][2], ("Extract mutations", ["mutation"], "records"))

    def test_mode_table_is_forwarded(self):
        response = self._post({
            "goal": "Extract table",
            "columns": ["mutation", "effect"],
            "mode": "table",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mode"], "table")
        self.assertEqual(self.calls[0][2], ("Extract table", ["mutation", "effect"], "table"))

    def test_reserved_columns_get_400(self):
        response = self._post({
            "goal": "Extract",
            "columns": ["mutation", "paper_key", "evidence"],
        })
        self.assertEqual(response.status_code, 400)
        message = response.get_json()["error"]
        self.assertIn("paper_key", message)
        self.assertIn("evidence", message)
        self.assertEqual(self.calls, [])

    def test_duplicate_columns_get_400(self):
        response = self._post({
            "goal": "Extract",
            "columns": ["mutation", "Mutation"],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Duplicate", response.get_json()["error"])
        self.assertEqual(self.calls, [])

    def test_invalid_mode_gets_400(self):
        response = self._post({"goal": "Extract", "columns": [], "mode": "figures"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown extraction mode", response.get_json()["error"])
        self.assertEqual(self.calls, [])


class ExtractJobMigrationTests(unittest.TestCase):
    def test_legacy_extracted_sheet_is_migrated_and_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            extracted = tmp / "extracted_data.xlsx"
            closed = tmp / "closed.xlsx"
            checkpoint = tmp / "checkpoint.json"

            make_workbook(
                extracted,
                ["doi", "source", "pdf_file", "pdf_source", "mutation"],
                [["10.1/OLD", "pubmed", "", "none", "M1"]],
            )

            paper = {
                "title": "Paper One",
                "doi": "10.1/new",
                "pmid": "555",
                "sources": ["pubmed"],
            }
            job = {
                "log": [],
                "progress": {"done": 0, "total": 0, "current": ""},
                "result": None,
                "status": "running",
                "error": None,
            }

            with mock.patch.object(web.config, "EXTRACTED_XLSX", extracted), \
                 mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", closed), \
                 mock.patch.object(web.config, "CHECKPOINT_JSON", checkpoint), \
                 mock.patch.object(web.config, "PDF_ARCHIVE_ENABLED", False), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(web, "load_settings", lambda: {}), \
                 mock.patch.object(web, "_load_search_results", lambda required=False: [paper]), \
                 mock.patch.object(
                     web,
                     "_prepare_paper",
                     lambda p: ("full text", "test", {
                         "path": None, "filename": "", "label": "none", "reason": "no PDF",
                     }),
                 ), \
                 mock.patch.object(
                     web.extractor,
                     "extract",
                     lambda text, fields=None, instruction=None: [{"mutation": "M2"}],
                 ):
                web._run_extract_job(job, "Extract mutations", ["mutation"], mode="records")

            self.assertEqual(job["status"], "done")
            table = read_sheet(extracted)
            header = table[0]
            self.assertIn("paper_key", header)
            self.assertIn("row_index", header)
            self.assertIn("evidence", header)
            self.assertIn("evidence_page", header)
            objects = [dict(zip(header, row)) for row in table[1:]]
            self.assertEqual(len(objects), 2)
            self.assertEqual(objects[0]["mutation"], "M1")
            self.assertEqual(objects[0]["doi"], "10.1/OLD")
            self.assertEqual(objects[0]["paper_key"], "doi:10.1/old")
            self.assertEqual(objects[1]["mutation"], "M2")
            self.assertEqual(objects[1]["paper_key"], "pmid:555")
            self.assertEqual(objects[1]["row_index"], 1)


if __name__ == "__main__":
    unittest.main()
