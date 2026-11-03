"""Flask API tests: legacy results and extraction payload compatibility."""

import json
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


class SearchApiJournalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.settings_path = self.dir / "web_settings.json"
        self.client = web.app.test_client()
        self.calls = []

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_start(self, job_type, fn, *args):
        self.calls.append((job_type, fn, args))
        return {"id": "job123"}, None

    def _post(self, payload):
        with mock.patch.object(web, "_start_job", new=self._fake_start), \
             mock.patch.object(web, "SETTINGS_PATH", self.settings_path):
            return self.client.post("/api/search", json=payload)

    def test_new_client_exclusions_are_parsed_forwarded_and_persisted(self):
        response = self._post({
            "query": "A AND B",
            "sources": ["pubmed"],
            "max_results": 5,
            "excluded_journals": "  Foo Journal \n\nfoo journal\n Bar Journal ",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[0][0], "search")
        self.assertEqual(
            self.calls[0][2],
            ("A AND B", ["pubmed"], 5, ["Foo Journal", "Bar Journal"]),
        )
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["excluded_journals"], ["Foo Journal", "Bar Journal"])

    def test_old_client_without_key_falls_back_to_saved_setting(self):
        self.settings_path.write_text(
            json.dumps({"excluded_journals": ["Saved Journal"]}), encoding="utf-8"
        )
        response = self._post({
            "query": "A",
            "sources": ["crossref"],
            "max_results": 3,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.calls[0][2],
            ("A", ["crossref"], 3, ["Saved Journal"]),
        )

    def test_old_client_without_settings_falls_back_to_config(self):
        with mock.patch.object(web.config, "EXCLUDED_JOURNALS", ["Configured Journal"]):
            response = self._post({
                "query": "A",
                "sources": ["europepmc"],
                "max_results": 4,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.calls[0][2],
            ("A", ["europepmc"], 4, ["Configured Journal"]),
        )
        self.assertFalse(self.settings_path.exists())

    def test_rejected_search_does_not_start_a_job(self):
        response = self._post({"query": "", "sources": ["pubmed"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])


class SettingsApiJournalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.settings_path = self.dir / "web_settings.json"
        self.client = web.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def _request(self, method, payload=None):
        with mock.patch.object(web, "SETTINGS_PATH", self.settings_path), \
             mock.patch.object(web, "apply_settings", lambda s: None):
            if method == "POST":
                return self.client.post("/api/settings", json=payload)
            return self.client.get("/api/settings")

    def test_excluded_journals_round_trip_as_text(self):
        response = self._request("POST", {
            "excluded_journals": "Journal One\n\nJOURNAL ONE\n Journal Two \n",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["excluded_journals"], ["Journal One", "Journal Two"])

        fetched = self._request("GET").get_json()
        self.assertEqual(fetched["excluded_journals"], ["Journal One", "Journal Two"])

    def test_excluded_journals_accepts_a_list(self):
        response = self._request("POST", {
            "excluded_journals": ["Journal One", "journal one", "Journal Two"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["excluded_journals"], ["Journal One", "Journal Two"])

    def test_missing_setting_defaults_to_config(self):
        with mock.patch.object(web.config, "EXCLUDED_JOURNALS", ["From Config"]):
            fetched = self._request("GET").get_json()
        self.assertEqual(fetched["excluded_journals"], ["From Config"])


class SearchJobJournalTests(unittest.TestCase):
    def _job(self):
        return {
            "log": [],
            "progress": {"done": 0, "total": 0, "current": ""},
            "result": None,
            "status": "running",
            "error": None,
        }

    def test_search_job_filters_results_and_reports_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            search = tmp / "search_results.xlsx"
            papers = [
                {"sources": ["pubmed"], "title": "Blocked", "doi": "10.1/blocked",
                 "journal": "Predatory Journal of Examples"},
                {"sources": ["pubmed"], "title": "Kept", "doi": "10.1/kept",
                 "journal": "Good Journal"},
                {"sources": ["pubmed"], "title": "No journal", "doi": "10.1/no-journal",
                 "journal": ""},
            ]
            job = self._job()
            with mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", search), \
                 mock.patch.object(web.config, "EXTRACTED_XLSX", tmp / "extracted.xlsx"), \
                 mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", tmp / "closed.xlsx"), \
                 mock.patch.object(web.config, "CHECKPOINT_JSON", tmp / "checkpoint.json"), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(web, "load_settings", lambda: {}), \
                 mock.patch.object(
                     web.searchers, "search",
                     lambda query, sources, limit=None: list(papers),
                 ):
                web._run_search_job(
                    job, "A", ["pubmed"], 10, ["predatory journal of examples"]
                )

            self.assertEqual(job["status"], "done")
            self.assertEqual(job["result"]["excluded"], 1)
            self.assertEqual(job["result"]["total"], 2)
            self.assertEqual(job["result"]["added"], 2)
            self.assertIn(
                "Excluded 1 paper(s) by journal blocklist.",
                "\n".join(job["log"]),
            )

            rows = read_sheet(search)
            header = rows[0]
            titles = [dict(zip(header, row))["title"] for row in rows[1:]]
            self.assertEqual(titles, ["Kept", "No journal"])

    def test_search_job_without_explicit_terms_uses_saved_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            search = tmp / "search_results.xlsx"
            papers = [
                {"sources": ["pubmed"], "title": "Blocked",
                 "journal": "Journal of Bad Results"},
            ]
            job = self._job()
            with mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", search), \
                 mock.patch.object(web.config, "EXTRACTED_XLSX", tmp / "extracted.xlsx"), \
                 mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", tmp / "closed.xlsx"), \
                 mock.patch.object(web.config, "CHECKPOINT_JSON", tmp / "checkpoint.json"), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(
                     web, "load_settings",
                     lambda: {"excluded_journals": ["bad results"]},
                 ), \
                 mock.patch.object(
                     web.searchers, "search",
                     lambda query, sources, limit=None: list(papers),
                 ):
                web._run_search_job(job, "A", ["pubmed"], 10)

            self.assertEqual(job["status"], "done")
            self.assertEqual(job["result"]["excluded"], 1)
            self.assertEqual(job["result"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
