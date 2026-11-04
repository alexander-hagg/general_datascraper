"""Flask API tests: legacy results and extraction payload compatibility."""

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import pymupdf
from openpyxl import Workbook

import app as web
import pdf_annotations


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


def patch_figures_paths(case, root):
    """Keep figure detection/cleanup inside a temp dir during a test."""
    root = Path(root)
    fig_dir = root / "figures"
    mapping = {
        "FIGURES_DIR": fig_dir,
        "FIGURES_INDEX_JSON": fig_dir / "index.json",
        "FIGURES_XLSX": root / "figures.xlsx",
    }
    for name, value in mapping.items():
        patcher = mock.patch.object(web.config, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)


def make_pdf(path, pages):
    """Create a small real PDF; `pages` is a list of lists of text lines."""
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        for index, line in enumerate(lines):
            page.insert_text((72, 72 + index * 24), line)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


class ResultsApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.search = self.dir / "search_results.xlsx"
        self.extracted = self.dir / "extracted_data.xlsx"
        self.closed = self.dir / "closed_source_papers.xlsx"
        self.index = self.dir / "pdf_index.json"
        self.client = web.app.test_client()
        patcher = mock.patch.object(web.config, "PDF_INDEX_JSON", self.index)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        self.assertEqual(
            set(payload), {"search", "extracted", "closed", "annotations", "figures"}
        )
        self.assertEqual(payload["annotations"], {})
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
    def setUp(self):
        # Existing job tests must never write the real output/prompt_history.json.
        patcher = mock.patch.object(web.history, "record")
        patcher.start()
        self.addCleanup(patcher.stop)

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
            self.assertFalse(job["result"]["cancelled"])
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
            ("A AND B", ["pubmed"], 5, ["Foo Journal", "Bar Journal"], None, None,
             "", False, None),
        )
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["excluded_journals"], ["Foo Journal", "Bar Journal"])

    def test_query_tree_is_forwarded_to_the_search_job(self):
        tree = {
            "type": "group",
            "op": "AND",
            "children": [
                {"type": "term", "text": "A", "op": "AND"},
                {
                    "type": "group",
                    "op": "NOT",
                    "children": [
                        {"type": "term", "text": "C", "op": "AND"},
                        {"type": "term", "text": "D", "op": "OR"},
                    ],
                },
            ],
        }
        response = self._post({
            "query": "A NOT (C OR D)",
            "sources": ["pubmed"],
            "query_tree": tree,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[0][2][-1], tree)

    def test_invalid_query_tree_does_not_fail_the_search(self):
        response = self._post({
            "query": "A",
            "sources": ["pubmed"],
            "query_tree": "not a tree",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.calls), 1)

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
            ("A", ["crossref"], 3, ["Saved Journal"], None, None, "", False, None),
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
            ("A", ["europepmc"], 4, ["Configured Journal"], None, None, "", False, None),
        )
        self.assertFalse(self.settings_path.exists())

    def test_rejected_search_does_not_start_a_job(self):
        response = self._post({"query": "", "sources": ["pubmed"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])


class LocalSearchApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.settings_path = self.dir / "web_settings.json"
        self.folder = self.dir / "pdfs"
        self.folder.mkdir()
        self.client = web.app.test_client()
        self.calls = []

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_start(self, job_type, fn, *args):
        self.calls.append((job_type, fn, args))
        return {"id": "job123"}, None

    def _post(self, payload):
        with mock.patch.object(web, "_start_job", new=self._fake_start), \
             mock.patch.object(web, "SETTINGS_PATH", self.settings_path), \
             mock.patch.object(web.config, "EXCLUDED_JOURNALS", []), \
             mock.patch.object(web.config, "YEAR_FROM", None), \
             mock.patch.object(web.config, "YEAR_TO", None), \
             mock.patch.object(web.config, "LOCAL_PDF_DIR", ""), \
             mock.patch.object(web.config, "LOCAL_PDF_RECURSIVE", False):
            return self.client.post("/api/search", json=payload)

    def test_folder_only_search_is_allowed(self):
        response = self._post({
            "query": "",
            "sources": [],
            "local_folder": str(self.folder),
            "local_recursive": True,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.calls[0][2],
            ("", [], 50, [], None, None, str(self.folder), True, None),
        )
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["local_folder"], str(self.folder))
        self.assertTrue(saved["local_recursive"])

    def test_explicit_folder_only_flag_ignores_online_fields(self):
        response = self._post({
            "query": "A",
            "sources": ["pubmed"],
            "local_folder": str(self.folder),
            "local_only": True,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.calls[0][2],
            ("", [], 50, [], None, None, str(self.folder), False, None),
        )

    def test_folder_only_flag_without_a_folder_gets_400(self):
        response = self._post({
            "query": "A",
            "sources": ["pubmed"],
            "local_folder": "",
            "local_only": True,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("folder-only", response.get_json()["error"])
        self.assertEqual(self.calls, [])

    def test_invalid_folder_gets_400_before_a_job_starts(self):
        response = self._post({
            "query": "",
            "sources": [],
            "local_folder": str(self.dir / "missing"),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("does not exist", response.get_json()["error"])
        self.assertEqual(self.calls, [])

    def test_folder_with_query_but_no_sources_is_rejected(self):
        response = self._post({
            "query": "A",
            "sources": [],
            "local_folder": str(self.folder),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose at least one website", response.get_json()["error"])
        self.assertEqual(self.calls, [])

    def test_folder_with_sources_but_no_query_is_rejected(self):
        response = self._post({
            "query": "",
            "sources": ["pubmed"],
            "local_folder": str(self.folder),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Enter at least one search term", response.get_json()["error"])
        self.assertEqual(self.calls, [])

    def test_without_folder_query_and_sources_are_still_required(self):
        no_query = self._post({"query": "", "sources": ["pubmed"]})
        no_sources = self._post({"query": "A", "sources": []})
        self.assertEqual(no_query.status_code, 400)
        self.assertIn("search term", no_query.get_json()["error"])
        self.assertEqual(no_sources.status_code, 400)
        self.assertIn("website", no_sources.get_json()["error"])
        self.assertEqual(self.calls, [])

    def test_explicit_blank_folder_clears_the_saved_setting(self):
        self.settings_path.write_text(
            json.dumps({"local_folder": str(self.folder), "local_recursive": True}),
            encoding="utf-8",
        )
        response = self._post({
            "query": "A", "sources": ["pubmed"],
            "local_folder": "", "local_recursive": False,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.calls[0][2],
            ("A", ["pubmed"], 50, [], None, None, "", False, None),
        )
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["local_folder"], "")
        self.assertFalse(saved["local_recursive"])

    def test_omitted_folder_falls_back_to_saved_setting(self):
        self.settings_path.write_text(
            json.dumps({"local_folder": str(self.folder), "local_recursive": True}),
            encoding="utf-8",
        )
        response = self._post({"query": "A", "sources": ["pubmed"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.calls[0][2],
            ("A", ["pubmed"], 50, [], None, None, str(self.folder), True, None),
        )


class SettingsApiLocalTests(unittest.TestCase):
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

    def test_local_folder_settings_round_trip(self):
        folder = str(self.dir / "pdfs")
        response = self._request("POST", {
            "local_folder": folder,
            "local_recursive": True,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["local_folder"], folder)
        self.assertTrue(response.get_json()["local_recursive"])

        fetched = self._request("GET").get_json()
        self.assertEqual(fetched["local_folder"], folder)
        self.assertTrue(fetched["local_recursive"])

    def test_missing_local_settings_default_to_config(self):
        with mock.patch.object(web.config, "LOCAL_PDF_DIR", "/configured/path"), \
             mock.patch.object(web.config, "LOCAL_PDF_RECURSIVE", True):
            fetched = self._request("GET").get_json()
        self.assertEqual(fetched["local_folder"], "/configured/path")
        self.assertTrue(fetched["local_recursive"])


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
    def setUp(self):
        patcher = mock.patch.object(web.history, "record")
        patcher.start()
        self.addCleanup(patcher.stop)
        self._fig_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._fig_tmp.cleanup)
        patch_figures_paths(self, self._fig_tmp.name)

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
                 mock.patch.object(
                     web.config, "LOCAL_PDF_INDEX_JSON", tmp / "local_pdfs.json"
                 ), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(web, "load_settings", lambda: {}), \
                 mock.patch.object(
                     web.searchers, "search",
                     lambda query, sources, limit=None, year_from=None, year_to=None,
                            cancel_check=None:
                         list(papers),
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
                 mock.patch.object(
                     web.config, "LOCAL_PDF_INDEX_JSON", tmp / "local_pdfs.json"
                 ), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(
                     web, "load_settings",
                     lambda: {"excluded_journals": ["bad results"]},
                 ), \
                 mock.patch.object(
                     web.searchers, "search",
                     lambda query, sources, limit=None, year_from=None, year_to=None,
                            cancel_check=None:
                         list(papers),
                 ):
                web._run_search_job(job, "A", ["pubmed"], 10)

            self.assertEqual(job["status"], "done")
            self.assertEqual(job["result"]["excluded"], 1)
            self.assertEqual(job["result"]["total"], 0)


class SearchJobLocalTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(web.history, "record")
        patcher.start()
        self.addCleanup(patcher.stop)
        self._fig_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._fig_tmp.cleanup)
        patch_figures_paths(self, self._fig_tmp.name)

    def _job(self):
        return {
            "log": [],
            "progress": {"done": 0, "total": 0, "current": ""},
            "result": None,
            "status": "running",
            "error": None,
        }

    def _stats(self, scanned=1):
        return {
            "folder": "/papers", "recursive": False, "found": scanned,
            "scanned": scanned, "copied": scanned, "linked": 0, "skipped": 0,
            "duplicates": 0, "errors": 0, "error_details": [], "papers": scanned,
        }

    def _local_paper(self, folder, filename="local_x.pdf"):
        path = folder / filename
        path.write_bytes(b"%PDF-1.4\nlocal test\n")
        return {
            "paper_key": "doi:10.1/local",
            "sources": ["local"],
            "title": "Local Paper",
            "doi": "10.1/local",
            "authors": "Local Author",
            "year": "2020",
            "journal": "",
            "is_oa": True,
            "local_pdf": filename,
            "local_pdf_path": str(path),
            "local_pdf_original": "/source/paper.pdf",
        }

    def test_folder_only_job_merges_local_papers_and_writes_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pdf_dir = tmp / "pdfs"
            pdf_dir.mkdir()
            local = self._local_paper(pdf_dir)
            job = self._job()

            with mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", tmp / "search.xlsx"), \
                 mock.patch.object(web.config, "EXTRACTED_XLSX", tmp / "extracted.xlsx"), \
                 mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", tmp / "closed.xlsx"), \
                 mock.patch.object(web.config, "CHECKPOINT_JSON", tmp / "checkpoint.json"), \
                 mock.patch.object(web.config, "PDF_DIR", pdf_dir), \
                 mock.patch.object(
                     web.config, "LOCAL_PDF_INDEX_JSON", tmp / "local_pdfs.json"
                 ), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(web, "load_settings", lambda: {}), \
                 mock.patch.object(
                     web.local_pdfs, "scan_folder",
                     lambda folder, recursive=False: ([local], self._stats()),
                 ), \
                 mock.patch.object(
                     web.searchers, "search",
                     side_effect=AssertionError("online search must be skipped"),
                 ):
                web._run_search_job(
                    job, "", [], 10, [], None, None, str(tmp), False
                )

            self.assertEqual(job["status"], "done")
            self.assertEqual(job["result"]["total"], 1)
            self.assertEqual(job["result"]["local_count"], 1)
            self.assertEqual(job["result"]["local_folder"], str(tmp))
            self.assertIn("folder-only", "\n".join(job["log"]))

            rows = read_sheet(tmp / "search.xlsx")
            self.assertEqual(dict(zip(rows[0], rows[1]))["title"], "Local Paper")
            index = json.loads(
                (tmp / "local_pdfs.json").read_text(encoding="utf-8")
            )
            self.assertIn("doi:10.1/local", index)

    def test_online_and_local_results_are_merged_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pdf_dir = tmp / "pdfs"
            pdf_dir.mkdir()
            online = {
                "paper_key": "doi:10.1/local",
                "sources": ["pubmed"],
                "title": "Local Paper",
                "doi": "10.1/LOCAL",
                "pmid": "",
                "year": "2020",
                "journal": "Journal",
                "authors": "",
            }
            local = self._local_paper(pdf_dir)
            job = self._job()

            with mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", tmp / "search.xlsx"), \
                 mock.patch.object(web.config, "EXTRACTED_XLSX", tmp / "extracted.xlsx"), \
                 mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", tmp / "closed.xlsx"), \
                 mock.patch.object(web.config, "CHECKPOINT_JSON", tmp / "checkpoint.json"), \
                 mock.patch.object(web.config, "PDF_DIR", pdf_dir), \
                 mock.patch.object(
                     web.config, "LOCAL_PDF_INDEX_JSON", tmp / "local_pdfs.json"
                 ), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(web, "load_settings", lambda: {}), \
                 mock.patch.object(
                     web.local_pdfs, "scan_folder",
                     lambda folder, recursive=False: ([local], self._stats()),
                 ), \
                 mock.patch.object(
                     web.searchers, "search",
                     lambda query, sources, limit=None, year_from=None, year_to=None,
                            cancel_check=None:
                         [dict(online)],
                 ):
                web._run_search_job(
                    job, "A", ["pubmed"], 10, [], None, None, str(tmp), False
                )

            self.assertEqual(job["status"], "done")
            self.assertEqual(job["result"]["total"], 1)
            rows = read_sheet(tmp / "search.xlsx")
            row = dict(zip(rows[0], rows[1]))
            self.assertEqual(row["title"], "Local Paper")
            self.assertIn("local", row["source"])
            index = json.loads(
                (tmp / "local_pdfs.json").read_text(encoding="utf-8")
            )
            self.assertIn("doi:10.1/local", index)

    def test_search_job_replaces_stale_index_even_without_local_pdfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            index_path = tmp / "local_pdfs.json"
            index_path.write_text(
                json.dumps({"doi:10.1/old": {"filename": "old.pdf"}}),
                encoding="utf-8",
            )
            job = self._job()

            with mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", tmp / "search.xlsx"), \
                 mock.patch.object(web.config, "EXTRACTED_XLSX", tmp / "extracted.xlsx"), \
                 mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", tmp / "closed.xlsx"), \
                 mock.patch.object(web.config, "CHECKPOINT_JSON", tmp / "checkpoint.json"), \
                 mock.patch.object(web.config, "LOCAL_PDF_INDEX_JSON", index_path), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(web, "load_settings", lambda: {}), \
                 mock.patch.object(
                     web.searchers, "search",
                     lambda query, sources, limit=None, year_from=None, year_to=None,
                            cancel_check=None: [],
                 ):
                web._run_search_job(job, "A", ["pubmed"], 10, [], None, None, "", False)

            self.assertEqual(job["status"], "done")
            self.assertEqual(
                json.loads(index_path.read_text(encoding="utf-8")), {}
            )


class HistoryApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "prompt_history.json"
        self._path_patch = mock.patch.object(
            web.config, "PROMPT_HISTORY_JSON", self.path
        )
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)
        self.client = web.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def test_history_lists_all_and_filtered_by_kind(self):
        search = web.history.record("search", {
            "query": "A AND B",
            "query_tree": {
                "type": "group", "op": "AND",
                "children": [{"type": "term", "text": "A", "op": "AND"}],
            },
        })
        goal = web.history.record("goal", {"text": "Find mutations"})

        payload = self.client.get("/api/history").get_json()
        self.assertEqual([e["id"] for e in payload["entries"]], [goal["id"], search["id"]])

        filtered = self.client.get("/api/history?kind=search").get_json()
        self.assertEqual([e["kind"] for e in filtered["entries"]], ["search"])
        self.assertEqual(filtered["entries"][0]["data"]["query"], "A AND B")

    def test_history_rejects_an_unknown_kind(self):
        response = self.client.get("/api/history?kind=bogus")
        self.assertEqual(response.status_code, 400)
        self.assertIn("history kind", response.get_json()["error"])

        response = self.client.post("/api/history/clear", json={"kind": "bogus"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/history/clear", json={"kind": 3})
        self.assertEqual(response.status_code, 400)

    def test_history_clear_one_kind_or_all(self):
        web.history.record("search", {"query": "A"})
        web.history.record("goal", {"text": "B"})
        web.history.record("columns", {"text": "C"})

        response = self.client.post("/api/history/clear", json={"kind": "goal"})
        self.assertEqual(response.get_json(), {"cleared": 1})
        self.assertEqual(len(self.client.get("/api/history").get_json()["entries"]), 2)

        response = self.client.post("/api/history/clear", json={})
        self.assertEqual(response.get_json(), {"cleared": 2})
        self.assertEqual(self.client.get("/api/history").get_json()["entries"], [])

    def test_history_file_is_not_exposed_by_download_routes(self):
        web.history.record("goal", {"text": "Private prompt"})
        self.assertTrue(self.path.exists())
        response = self.client.get("/download/prompt_history.json")
        self.assertEqual(response.status_code, 404)


class SearchJobHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "prompt_history.json"
        self._path_patch = mock.patch.object(
            web.config, "PROMPT_HISTORY_JSON", self.path
        )
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)
        patch_figures_paths(self, self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _job(self):
        return {
            "log": [],
            "progress": {"done": 0, "total": 0, "current": ""},
            "result": None,
            "status": "running",
            "error": None,
        }

    def _run(self, query_tree=None, fail_history=False):
        folder = self.dir / "pdfs"
        folder.mkdir(exist_ok=True)
        stats = {
            "folder": str(folder), "recursive": True, "found": 0,
            "scanned": 0, "copied": 0, "linked": 0, "skipped": 0,
            "duplicates": 0, "errors": 0, "error_details": [], "papers": 0,
        }
        job = self._job()
        patches = [
            mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", self.dir / "search.xlsx"),
            mock.patch.object(web.config, "EXTRACTED_XLSX", self.dir / "extracted.xlsx"),
            mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", self.dir / "closed.xlsx"),
            mock.patch.object(web.config, "CHECKPOINT_JSON", self.dir / "checkpoint.json"),
            mock.patch.object(
                web.config, "LOCAL_PDF_INDEX_JSON", self.dir / "local_pdfs.json"
            ),
            mock.patch.object(web, "apply_settings", lambda s: None),
            mock.patch.object(web, "load_settings", lambda: {}),
            mock.patch.object(
                web.local_pdfs, "scan_folder",
                lambda folder, recursive=False: ([], stats),
            ),
            mock.patch.object(
                web.searchers, "search",
                lambda query, sources, limit=None, year_from=None, year_to=None,
                       cancel_check=None: [],
            ),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            if fail_history:
                stack.enter_context(
                    mock.patch.object(
                        web.history, "record",
                        side_effect=RuntimeError("history disk is full"),
                    )
                )
            web._run_search_job(
                job, "  A AND B  ", ["pubmed"], 12,
                ["Blocked Journal"], "2015", "2020", str(folder), True,
                query_tree,
            )
        return job

    def test_search_job_records_the_normalized_prompt(self):
        tree = {
            "type": "group",
            "op": "AND",
            "children": [
                {"type": "term", "text": "A", "op": "AND"},
                {"type": "term", "text": "B", "op": "AND"},
                {
                    "type": "group",
                    "op": "NOT",
                    "children": [
                        {"type": "term", "text": "C", "op": "AND"},
                        {"type": "term", "text": "D", "op": "OR"},
                    ],
                },
            ],
        }
        job = self._run(query_tree=tree)
        self.assertEqual(job["status"], "done")

        entries = web.history.list_entries("search")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["data"], {
            "query": "A AND B",
            "query_tree": tree,
            "sources": ["pubmed"],
            "max_results": 12,
            "year_from": "2015",
            "year_to": "2020",
            "excluded_journals": ["Blocked Journal"],
            "local_folder": str(self.dir / "pdfs"),
            "local_recursive": True,
        })
        # No credentials or backend settings are stored.
        for secret in ("email", "model", "api_key", "openai_base_url"):
            self.assertNotIn(secret, entries[0]["data"])

    def test_history_failure_does_not_break_the_search(self):
        job = self._run(fail_history=True)
        self.assertEqual(job["status"], "done")
        self.assertIn(
            "Warning: Could not save search prompt history",
            "\n".join(job["log"]),
        )
        self.assertFalse(self.path.exists())


class ExtractJobHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "prompt_history.json"
        self._path_patch = mock.patch.object(
            web.config, "PROMPT_HISTORY_JSON", self.path
        )
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _job(self):
        return {
            "log": [],
            "progress": {"done": 0, "total": 0, "current": ""},
            "result": None,
            "status": "running",
            "error": None,
        }

    def _run(self, goal, columns, fail_history=False):
        job = self._job()
        with mock.patch.object(web, "apply_settings", lambda s: None), \
             mock.patch.object(web, "load_settings", lambda: {}), \
             mock.patch.object(web, "_load_search_results", lambda required=False: []):
            if fail_history:
                with mock.patch.object(
                    web.history, "record",
                    side_effect=RuntimeError("history disk is full"),
                ):
                    web._run_extract_job(job, goal, columns, mode="records")
            else:
                web._run_extract_job(job, goal, columns, mode="records")
        return job

    def test_extract_job_records_goal_and_non_empty_columns(self):
        job = self._run("Find mutations", ["mutation", "effect"])
        self.assertEqual(job["status"], "done")

        goals = web.history.list_entries("goal")
        self.assertEqual([e["data"]["text"] for e in goals], ["Find mutations"])
        columns = web.history.list_entries("columns")
        self.assertEqual(
            [e["data"]["text"] for e in columns], ["mutation, effect"]
        )

    def test_extract_job_without_columns_records_only_the_goal(self):
        job = self._run("Find mutations", [])
        self.assertEqual(job["status"], "done")
        self.assertEqual(len(web.history.list_entries("goal")), 1)
        self.assertEqual(web.history.list_entries("columns"), [])

    def test_history_failure_does_not_break_extraction(self):
        job = self._run("Find mutations", ["mutation"], fail_history=True)
        self.assertEqual(job["status"], "done")
        self.assertIn(
            "Warning: Could not save goal prompt history",
            "\n".join(job["log"]),
        )
        self.assertFalse(self.path.exists())


class AnnotationsApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.pdf_dir = self.dir / "pdfs"
        self.pdf_dir.mkdir()
        self.annotated_dir = self.dir / "annotated_pdfs"
        self.index = self.dir / "pdf_index.json"
        self.source = make_pdf(
            self.pdf_dir / "PMID_1.pdf",
            [["Introduction only."],
             ["The mutation increased current in oocytes.", "Second sentence."]],
        )
        self.key = "pmid:1"
        self.client = web.app.test_client()
        for name, value in (
            ("PDF_DIR", self.pdf_dir),
            ("ANNOTATED_PDF_DIR", self.annotated_dir),
            ("PDF_INDEX_JSON", self.index),
        ):
            patcher = mock.patch.object(web.config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _annotate(self, quotes):
        rows = [{"row_index": i, "evidence": q} for i, q in enumerate(quotes, 1)]
        return pdf_annotations.annotate_paper_rows(
            rows,
            self.source,
            paper_key=self.key,
            title="Paper",
            pdf_file=self.source.name,
            pdf_source="article:pmc",
        )

    def _url(self, key=None):
        return "/api/annotated-pdf/" + quote(key or self.key, safe="")

    @staticmethod
    def _annotation_count(pdf_bytes):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(pdf_bytes)
            handle.flush()
            doc = pymupdf.open(handle.name)
            try:
                return sum(len(list(page.annots())) for page in doc)
            finally:
                doc.close()

    def test_serves_existing_annotation_inline(self):
        _rows, summary = self._annotate(["mutation increased current"])
        self.assertTrue(summary["annotation_file"])
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertGreaterEqual(self._annotation_count(response.data), 1)

    def test_regenerates_a_missing_annotation(self):
        _rows, summary = self._annotate(["mutation increased current"])
        annotation = self.annotated_dir / summary["annotation_file"]
        self.assertTrue(annotation.exists())
        annotation.unlink()
        self.assertFalse(annotation.exists())

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertTrue(annotation.exists())
        self.assertGreaterEqual(self._annotation_count(response.data), 1)

    def test_serves_original_when_no_evidence_resolves(self):
        _rows, summary = self._annotate(["a sentence that is not in the paper"])
        self.assertEqual(summary["annotation_file"], "")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, self.source.read_bytes())

    def test_unknown_key_and_traversal_are_404(self):
        self.assertEqual(self.client.get(self._url("pmid:missing")).status_code, 404)
        self.assertEqual(
            self.client.get("/api/annotated-pdf/" + quote("../etc/passwd", safe="")).status_code,
            404,
        )

        self.index.write_text(json.dumps({
            "pmid:1": {
                "paper_key": "pmid:1",
                "pdf_file": "../secret.pdf",
                "pdf_source": "article:pmc",
                "title": "Paper",
                "annotation_file": "",
                "annotation_hash": "",
                "evidence": [],
            }
        }), encoding="utf-8")
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_results_include_api_safe_annotations(self):
        self._annotate(["mutation increased current", "missing quote"])
        extracted = self.dir / "extracted_data.xlsx"
        make_workbook(
            extracted,
            ["paper_key", "row_index", "evidence", "evidence_page", "mutation"],
            [["pmid:1", 1, "mutation increased current", 2, "M1"]],
        )
        with mock.patch.object(web.config, "EXTRACTED_XLSX", extracted), \
             mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", self.dir / "missing.xlsx"), \
             mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", self.dir / "missing2.xlsx"):
            payload = self.client.get("/api/results").get_json()

        self.assertEqual(
            set(payload), {"search", "extracted", "closed", "annotations", "figures"}
        )
        annotation = payload["annotations"][self.key]
        self.assertTrue(annotation["available"])
        self.assertEqual(annotation["pdf_file"], "PMID_1.pdf")
        self.assertEqual(annotation["pdf_source"], "article:pmc")
        self.assertEqual(
            [item["status"] for item in annotation["evidence"]],
            ["exact", "unresolved"],
        )
        serialized = json.dumps(payload)
        self.assertNotIn(str(self.dir), serialized)


class ExtractJobEvidenceTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(web.history, "record")
        patcher.start()
        self.addCleanup(patcher.stop)
        self._fig_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._fig_tmp.cleanup)
        patch_figures_paths(self, self._fig_tmp.name)

    def _job(self):
        return {
            "log": [],
            "progress": {"done": 0, "total": 0, "current": ""},
            "result": None,
            "status": "running",
            "error": None,
        }

    def _run(self, annotate):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            extracted = tmp / "extracted_data.xlsx"
            job = self._job()
            paper = {
                "title": "Paper One",
                "doi": "10.1/new",
                "pmid": "555",
                "sources": ["pubmed"],
            }
            pdf_path = tmp / "pdfs" / "PMID_555.pdf"
            pdf_path.parent.mkdir()
            make_pdf(pdf_path, [["The mutation changed the current."]])
            with mock.patch.object(web.config, "EXTRACTED_XLSX", extracted), \
                 mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", tmp / "closed.xlsx"), \
                 mock.patch.object(web.config, "CHECKPOINT_JSON", tmp / "checkpoint.json"), \
                 mock.patch.object(web.config, "PDF_ARCHIVE_ENABLED", False), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(web, "load_settings", lambda: {}), \
                 mock.patch.object(web, "_load_search_results", lambda required=False: [paper]), \
                 mock.patch.object(
                     web,
                     "_prepare_paper",
                     lambda p: ("full text " * 100, "test", {
                         "path": pdf_path,
                         "filename": pdf_path.name,
                         "label": "article:pmc",
                         "reason": "",
                     }),
                 ), \
                 mock.patch.object(
                     web.extractor,
                     "extract",
                     lambda text, fields=None, instruction=None:
                         [{"mutation": "M2", "evidence": "mutation changed the current"}],
                 ), \
                 mock.patch.object(web.pdf_annotations, "annotate_paper_rows", side_effect=annotate):
                web._run_extract_job(job, "Extract mutations", ["mutation"], mode="records")
            return job, read_sheet(extracted)

    def test_extract_job_updates_evidence_page_and_reports_counts(self):
        def fake(rows, path, **kwargs):
            rows[0]["evidence_page"] = "1"
            return rows, {
                "located": 1, "unresolved": 0, "warning": "",
                "evidence": [],
            }

        job, table = self._run(fake)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["evidence_located"], 1)
        self.assertEqual(job["result"]["evidence_unresolved"], 0)
        self.assertIn("1 evidence quote(s) located, 0 unresolved", "\n".join(job["log"]))
        objects = [dict(zip(table[0], row)) for row in table[1:]]
        self.assertEqual(objects[0]["evidence"], "mutation changed the current")
        self.assertEqual(objects[0]["evidence_page"], "1")

    def test_annotation_failure_only_warns_and_keeps_blank_page(self):
        job, table = self._run(RuntimeError("broken PDF"))
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["annotation_warnings"], 1)
        self.assertIn("evidence warning: broken PDF", "\n".join(job["log"]))
        objects = [dict(zip(table[0], row)) for row in table[1:]]
        self.assertEqual(objects[0]["evidence"], "mutation changed the current")
        self.assertIn(objects[0]["evidence_page"], (None, ""))


class JobCancelApiTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        self.job = {
            "id": "canceljob",
            "type": "search",
            "status": "running",
            "error": None,
            "progress": {"done": 0, "total": 0, "current": ""},
            "log": [],
            "result": None,
            "cancel_requested": False,
        }
        web.JOBS[self.job["id"]] = self.job
        self.addCleanup(web.JOBS.pop, self.job["id"], None)

    def test_job_public_exposes_the_cancel_flag(self):
        self.assertFalse(web._job_public(self.job)["cancel_requested"])
        self.job["cancel_requested"] = True
        self.assertTrue(web._job_public(self.job)["cancel_requested"])

    def test_only_running_jobs_are_active(self):
        with mock.patch.object(web, "JOBS", {self.job["id"]: self.job}):
            self.assertIs(web._active_job(), self.job)
            self.job["status"] = "cancelled"
            self.assertIsNone(web._active_job())

    def test_unknown_job_gets_404(self):
        response = self.client.post("/api/job/missing/cancel")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {"error": "unknown job"})

    def test_finished_job_gets_409(self):
        self.job["status"] = "done"
        response = self.client.post("/api/job/canceljob/cancel")
        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload["error"], "job is not running")
        self.assertEqual(payload["status"], "done")
        self.assertFalse(self.job["cancel_requested"])

    def test_running_job_accepts_cancel_and_repeated_requests(self):
        first = self.client.post("/api/job/canceljob/cancel")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json(), {
            "cancelled": True,
            "status": "running",
            "cancel_requested": True,
        })
        self.assertTrue(self.job["cancel_requested"])
        self.assertIn(
            "Cancel requested; stopping after the current step.",
            self.job["log"],
        )

        second = self.client.post("/api/job/canceljob/cancel")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["cancel_requested"], True)
        self.assertEqual(self.job["status"], "running")
        self.assertEqual(
            self.job["log"].count(
                "Cancel requested; stopping after the current step."
            ),
            1,
        )


class SearchJobCancelTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(web.history, "record")
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.search = self.dir / "search_results.xlsx"
        self.extracted = self.dir / "extracted_data.xlsx"
        self.closed = self.dir / "closed_source_papers.xlsx"
        self.checkpoint = self.dir / "checkpoint.json"
        self.local_index = self.dir / "local_pdfs.json"
        self._fig_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._fig_tmp.cleanup)
        patch_figures_paths(self, self._fig_tmp.name)
        self.figures_dir = Path(self._fig_tmp.name) / "figures"
        self.figures_index = self.figures_dir / "index.json"
        self.figures_xlsx = Path(self._fig_tmp.name) / "figures.xlsx"

    def _job(self):
        return {
            "log": [],
            "progress": {"done": 0, "total": 0, "current": ""},
            "result": None,
            "status": "running",
            "error": None,
            "cancel_requested": False,
        }

    def _write_previous_dataset(self):
        self.search.write_bytes(b"previous search workbook")
        self.extracted.write_bytes(b"previous extracted workbook")
        self.closed.write_bytes(b"previous closed workbook")
        self.checkpoint.write_text(
            json.dumps({"pmid:1": {"status": "extracted", "n": 3}}),
            encoding="utf-8",
        )
        self.local_index.write_text(
            json.dumps({"pmid:1": {"filename": "old.pdf"}}),
            encoding="utf-8",
        )
        self.figures_dir.mkdir(parents=True, exist_ok=True)
        self.figures_index.write_text(
            json.dumps({"version": 1, "figures": [{"id": "old"}]}),
            encoding="utf-8",
        )
        self.figures_xlsx.write_bytes(b"previous figures workbook")

    def _snapshot(self):
        paths = [
            self.search, self.extracted, self.closed, self.checkpoint,
            self.local_index, self.figures_index, self.figures_xlsx,
        ]
        return {path: path.read_bytes() for path in paths}

    def _run(self, job, search, scan=None, **kwargs):
        patches = [
            mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", self.search),
            mock.patch.object(web.config, "EXTRACTED_XLSX", self.extracted),
            mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", self.closed),
            mock.patch.object(web.config, "CHECKPOINT_JSON", self.checkpoint),
            mock.patch.object(
                web.config, "LOCAL_PDF_INDEX_JSON", self.local_index
            ),
            mock.patch.object(web, "apply_settings", lambda s: None),
            mock.patch.object(web, "load_settings", lambda: {}),
            mock.patch.object(web.searchers, "search", search),
        ]
        if scan is not None:
            patches.append(mock.patch.object(web.local_pdfs, "scan_folder", scan))
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            web._run_search_job(job, "A", ["pubmed"], 10, **kwargs)

    def test_cancel_before_search_keeps_the_previous_dataset(self):
        self._write_previous_dataset()
        before = self._snapshot()
        job = self._job()
        job["cancel_requested"] = True
        search = mock.Mock(side_effect=AssertionError("search must not start"))

        self._run(job, search)

        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(job["error"])
        self.assertEqual(job["result"], {"cancelled": True})
        self.assertIn(
            "Search cancelled; previous results were kept.",
            "\n".join(job["log"]),
        )
        search.assert_not_called()
        self.assertEqual(self._snapshot(), before)

    def test_cancel_during_local_scan_takes_effect_after_the_scan(self):
        self._write_previous_dataset()
        before = self._snapshot()
        job = self._job()
        search = mock.Mock(side_effect=AssertionError("search must not start"))

        def fake_scan(folder, recursive=False):
            job["cancel_requested"] = True
            return [], {
                "scanned": 0, "copied": 0, "linked": 0, "duplicates": 0,
                "errors": 0, "error_details": [],
            }

        self._run(
            job, search, scan=fake_scan,
            local_folder=str(self.dir), local_recursive=False,
        )

        self.assertEqual(job["status"], "cancelled")
        search.assert_not_called()
        self.assertEqual(self._snapshot(), before)

    def test_cancel_after_online_search_does_not_replace_outputs(self):
        self._write_previous_dataset()
        before = self._snapshot()
        job = self._job()

        def fake_search(query, sources, limit=None, year_from=None, year_to=None,
                        cancel_check=None):
            self.assertTrue(callable(cancel_check))
            job["cancel_requested"] = True
            return [{"sources": ["pubmed"], "title": "New", "doi": "10.1/new"}]

        self._run(job, fake_search)

        self.assertEqual(job["status"], "cancelled")
        self.assertIn(
            "Search cancelled; previous results were kept.",
            "\n".join(job["log"]),
        )
        self.assertEqual(self._snapshot(), before)

    def test_search_cancelled_exception_marks_cancelled_not_error(self):
        self._write_previous_dataset()
        before = self._snapshot()
        job = self._job()

        def fake_search(*args, **kwargs):
            job["cancel_requested"] = True
            raise web.searchers.SearchCancelled()

        self._run(job, fake_search)

        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(job["error"])
        self.assertIn(
            "Search cancelled; previous results were kept.",
            "\n".join(job["log"]),
        )
        self.assertEqual(self._snapshot(), before)


class ExtractJobCancelTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(web.history, "record")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cancel_after_first_paper_keeps_completed_rows_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            extracted = tmp / "extracted_data.xlsx"
            closed = tmp / "closed.xlsx"
            checkpoint = tmp / "checkpoint.json"
            papers = [
                {"title": "Paper One", "doi": "10.1/one", "pmid": "555",
                 "sources": ["pubmed"]},
                {"title": "Paper Two", "doi": "10.1/two", "pmid": "666",
                 "sources": ["pubmed"]},
            ]
            job = {
                "log": [],
                "progress": {"done": 0, "total": 0, "current": ""},
                "result": None,
                "status": "running",
                "error": None,
                "cancel_requested": False,
            }

            def fake_extract(text, fields=None, instruction=None):
                job["cancel_requested"] = True
                return [{"mutation": "M2"}]

            prepare = mock.Mock(side_effect=lambda p: (
                "full text", "test",
                {"path": None, "filename": "", "label": "none", "reason": "no PDF"},
            ))
            with mock.patch.object(web.config, "EXTRACTED_XLSX", extracted), \
                 mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", closed), \
                 mock.patch.object(web.config, "CHECKPOINT_JSON", checkpoint), \
                 mock.patch.object(web.config, "PDF_ARCHIVE_ENABLED", False), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(web, "load_settings", lambda: {}), \
                 mock.patch.object(
                     web, "_load_search_results", lambda required=False: papers
                 ), \
                 mock.patch.object(web, "_prepare_paper", prepare), \
                 mock.patch.object(web.extractor, "extract", fake_extract):
                web._run_extract_job(
                    job, "Extract mutations", ["mutation"], mode="records"
                )

            self.assertEqual(job["status"], "cancelled")
            self.assertIsNone(job["error"])
            self.assertEqual(job["progress"]["done"], 1)
            self.assertEqual(job["progress"]["total"], 2)
            self.assertEqual(job["progress"]["current"], "")
            self.assertEqual(job["result"]["papers_processed"], 1)
            self.assertEqual(job["result"]["extracted"], 1)
            self.assertTrue(job["result"]["cancelled"])
            self.assertIn(
                "Extraction cancelled after 1 paper(s); completed rows were kept.",
                "\n".join(job["log"]),
            )
            self.assertEqual(prepare.call_count, 1)

            table = read_sheet(extracted)
            self.assertEqual(len(table), 2)
            row = dict(zip(table[0], table[1]))
            self.assertEqual(row["mutation"], "M2")
            self.assertEqual(row["paper_key"], "pmid:555")

            state = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(state["pmid:555"]["status"], "extracted")
            self.assertNotIn("pmid:666", state)


class SuggestGoalApiTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()

    def _post(self, payload):
        return self.client.post("/api/suggest-goal", json=payload)

    def test_success_returns_metadata_without_starting_a_job_or_history(self):
        jobs_before = set(web.JOBS)
        with mock.patch.object(
            web.suggester, "suggest_goal", return_value=["Goal A", "Goal B"]
        ) as suggest, \
             mock.patch.object(
                 web.suggester,
                 "llm_source",
                 return_value={"source": "llm", "model": "test-model"},
             ), \
             mock.patch.object(web, "_start_job") as start_job, \
             mock.patch.object(web.history, "record") as record:
            response = self._post({
                "goal": "  Extract mutations ",
                "columns": ["mutation", "effect"],
                "mode": "table",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "source": "llm",
            "model": "test-model",
            "suggestions": ["Goal A", "Goal B"],
        })
        suggest.assert_called_once_with(
            "Extract mutations",
            columns=["mutation", "effect"],
            mode="table",
        )
        start_job.assert_not_called()
        record.assert_not_called()
        self.assertEqual(set(web.JOBS), jobs_before)

    def test_empty_goal_gets_400_without_calling_the_llm(self):
        for payload in ({"goal": ""}, {"goal": "   "}, {}):
            with self.subTest(payload=payload):
                with mock.patch.object(web.suggester, "suggest_goal") as suggest:
                    response = self._post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("Describe what data you want", response.get_json()["error"])
                suggest.assert_not_called()

    def test_backend_unavailable_gets_503(self):
        with mock.patch.object(
            web.suggester,
            "suggest_goal",
            side_effect=web.suggester.BackendUnavailable("Ollama is not running."),
        ):
            response = self._post({"goal": "Extract mutations"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "Ollama is not running.")

    def test_value_error_gets_400(self):
        with mock.patch.object(
            web.suggester,
            "suggest_goal",
            side_effect=ValueError("Unknown extraction mode 'figures'."),
        ):
            response = self._post({"goal": "Extract mutations", "mode": "figures"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown extraction mode", response.get_json()["error"])

    def test_unexpected_failure_gets_502(self):
        with mock.patch.object(
            web.suggester,
            "suggest_goal",
            side_effect=RuntimeError("parse exploded"),
        ):
            response = self._post({"goal": "Extract mutations"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"],
            "Could not improve the goal: parse exploded",
        )


class SuggestGoalMarkupTests(unittest.TestCase):
    def test_button_panel_and_script_are_wired_after_the_goal(self):
        root = Path(__file__).resolve().parent.parent
        template = (root / "templates" / "index.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")

        goal_at = template.index('id="goal"')
        button_at = template.index('id="improve-goal"')
        panel_at = template.index('id="goal-suggest-panel"')
        columns_at = template.index('for="columns"')
        self.assertLess(goal_at, button_at)
        self.assertLess(button_at, panel_at)
        self.assertLess(panel_at, columns_at)
        self.assertIn("✨ Improve goal", template)
        self.assertIn(
            'aria-label="Improve the extraction goal with the LLM"', template
        )
        self.assertIn("/api/suggest-goal", script)
        self.assertIn("Use this goal", script)
        self.assertIn("improveGoal", script)
        self.assertIn("applyGoalSuggestion", script)


class DataTabViewerMarkupTests(unittest.TestCase):
    def test_viewer_markup_and_api_plumbing_exist(self):
        root = Path(__file__).resolve().parent.parent
        template = (root / "templates" / "index.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="pdf-viewer"', template)
        self.assertIn('id="pdf-new-tab"', template)
        self.assertIn("No archived PDF for this paper", template)
        self.assertIn("/api/annotated-pdf/", script)
        self.assertIn("data.annotations", script)
        self.assertIn("evidenceMetadataForRow", script)
        self.assertIn('id="figures-gallery"', template)
        self.assertIn('id="figure-modal"', template)
        self.assertIn('id="dl-figures"', template)
        self.assertIn("renderFigures", script)
        self.assertIn("data.figures", script)
        self.assertIn("/api/figure/", script)
        self.assertIn("figures.xlsx", template)
        self.assertIn('id="figure-modal-reason"', template)
        self.assertIn("Figures and tables are selected from this goal", template)
        self.assertIn("fig.reason", script)
        self.assertIn('id="cancel-job"', template)
        self.assertIn('aria-label="Cancel the running job"', template)
        self.assertIn("cancelJob", script)
        self.assertIn("/cancel", script)
        self.assertIn("Cancelling…", script)
        self.assertIn("Job cancelled.", script)


if __name__ == "__main__":
    unittest.main()
