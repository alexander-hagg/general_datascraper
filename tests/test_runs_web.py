"""Web/API tests for run folders, search refresh and state bookkeeping."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

import app as web
import config
import runs


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


class RunsWebBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.runs_dir = self.root / "runs"
        self.marker = self.root / "active_run.json"
        self.settings_path = self.root / "web_settings.json"
        self.client = web.app.test_client()

        self._saved_paths = runs.capture_paths()
        self._patchers = [
            mock.patch.object(config, "RUNS_ENABLED", True),
            mock.patch.object(config, "RUNS_DIR", self.runs_dir),
            mock.patch.object(config, "ACTIVE_RUN_JSON", self.marker),
            mock.patch.object(config, "OUTPUT_DIR", self.output),
            mock.patch.object(web, "SETTINGS_PATH", self.settings_path),
            mock.patch.object(web.history, "record"),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        try:
            runs.apply_legacy_paths()
        except Exception:
            pass
        for patcher in reversed(self._patchers):
            patcher.stop()
        runs.restore_paths(self._saved_paths)
        self._tmp.cleanup()

    def _job(self, cancel=False):
        return {
            "log": [],
            "progress": {"done": 0, "total": 0, "current": ""},
            "result": None,
            "status": "running",
            "error": None,
            "cancel_requested": cancel,
        }

    def _paper(self, title="Paper One", doi="10.1/one", pmid="111"):
        return {
            "sources": ["pubmed"],
            "title": title,
            "doi": doi,
            "pmid": pmid,
            "authors": "Author",
            "year": "2020",
            "journal": "Journal",
            "publication_type": "journal article",
        }

    def _search(self, papers=None, merge=False, query_tree=None,
                query_selection=None, job=None, search=None):
        job = job or self._job()
        papers = [self._paper()] if papers is None else papers
        search = search or (
            lambda query, sources, limit=None, year_from=None, year_to=None,
                   cancel_check=None: list(papers)
        )
        with mock.patch.object(web, "load_settings", lambda: {
            "email": "a@example.org", "model": "test-model",
            "excluded_journals": [],
        }), mock.patch.object(web, "apply_settings", lambda s: None), \
             mock.patch.object(web.searchers, "search", search):
            web._run_search_job(
                job, "A", ["pubmed"], 10, [], None, None, "", False,
                query_tree, query_selection, merge,
            )
        return job


class RunsDisabledApiTests(RunsWebBase):
    def test_runs_endpoints_report_disabled(self):
        with mock.patch.object(config, "RUNS_ENABLED", False):
            payload = self.client.get("/api/runs").get_json()
            self.assertEqual(payload, {"enabled": False, "active": None, "runs": []})
            payload = self.client.get("/api/run").get_json()
            self.assertEqual(
                payload, {"enabled": False, "run": None, "state": None}
            )

    def test_merge_search_is_rejected_when_runs_are_disabled(self):
        calls = []
        with mock.patch.object(config, "RUNS_ENABLED", False), \
             mock.patch.object(
                 web, "_start_job",
                 lambda *args: calls.append(args) or ({"id": "x"}, None),
             ):
            response = self.client.post("/api/search", json={
                "query": "A", "sources": ["pubmed"], "merge": True,
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("disabled", response.get_json()["error"])
        self.assertEqual(calls, [])


class RunsApiTests(RunsWebBase):
    def test_runs_and_run_endpoints_return_active_state(self):
        run_id = runs.create_run("Saved", {
            "query": {"rendered": "A AND B", "tree": {
                "type": "group", "op": "AND", "children": [],
            }},
            "counts": {"papers": 2},
        })
        runs.activate_run(run_id)

        listed = self.client.get("/api/runs").get_json()
        self.assertTrue(listed["enabled"])
        self.assertEqual(listed["active"]["id"], run_id)
        self.assertEqual([item["id"] for item in listed["runs"]], [run_id])
        self.assertEqual(listed["active"]["query"], "A AND B")
        self.assertNotIn("state", listed["active"])

        payload = self.client.get("/api/run").get_json()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["run"]["id"], run_id)
        self.assertEqual(payload["state"]["query"]["rendered"], "A AND B")

    def test_load_returns_state_without_starting_a_job(self):
        run_id = runs.create_run("Saved", {"sources": ["crossref"]})
        before = set(web.JOBS)
        response = self.client.post(f"/api/runs/{run_id}/load")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["run"]["id"], run_id)
        self.assertEqual(payload["state"]["sources"], ["crossref"])
        self.assertEqual(set(web.JOBS), before)
        self.assertEqual(runs.current_run_id(), run_id)
        self.assertEqual(config.SEARCH_RESULTS_XLSX.parent, self.runs_dir / run_id)

    def test_invalid_run_id_and_unknown_run_are_404(self):
        self.assertEqual(
            self.client.post("/api/runs/..%2Fescape/load").status_code, 404
        )
        self.assertEqual(
            self.client.post("/api/runs/20200101-000000-nope/load").status_code, 404
        )

    def test_load_and_delete_are_refused_while_a_job_runs(self):
        run_id = runs.create_run("Saved")
        job = {
            "id": "runningjob", "type": "search", "status": "running",
            "error": None, "progress": {"done": 0, "total": 0, "current": ""},
            "log": [], "result": None, "cancel_requested": False,
        }
        web.JOBS[job["id"]] = job
        self.addCleanup(web.JOBS.pop, job["id"], None)

        self.assertEqual(
            self.client.post(f"/api/runs/{run_id}/load").status_code, 409
        )
        self.assertEqual(
            self.client.post(f"/api/runs/{run_id}/delete").status_code, 409
        )

    def test_runs_endpoint_does_not_switch_paths_under_a_running_job(self):
        previous = runs.create_run("Previous")
        runs.activate_run(previous)
        new_run = runs.create_run("In progress")
        # Simulate a search job that has already switched to its new folder.
        runs.apply_run_paths(new_run)
        job = {
            "id": "runningjob2", "type": "search", "status": "running",
            "error": None, "progress": {"done": 0, "total": 0, "current": ""},
            "log": [], "result": None, "cancel_requested": False,
        }
        web.JOBS[job["id"]] = job
        self.addCleanup(web.JOBS.pop, job["id"], None)

        payload = self.client.get("/api/runs").get_json()
        self.assertTrue(payload["enabled"])
        self.assertEqual(config.SEARCH_RESULTS_XLSX.parent, self.runs_dir / new_run)
        self.assertEqual(runs.current_run_id(), previous)

    def test_delete_refuses_active_run_and_deletes_another(self):
        active = runs.create_run("Active")
        other = runs.create_run("Other")
        runs.activate_run(active)

        refused = self.client.post(f"/api/runs/{active}/delete")
        self.assertEqual(refused.status_code, 409)
        self.assertIn("active", refused.get_json()["error"])

        deleted = self.client.post(f"/api/runs/{other}/delete")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json(), {"deleted": other})
        self.assertFalse((self.runs_dir / other).exists())
        self.assertEqual(runs.current_run_id(), active)

    def test_merge_without_an_active_run_is_400_before_starting(self):
        calls = []
        with mock.patch.object(
            web, "_start_job",
            lambda *args: calls.append(args) or ({"id": "x"}, None),
        ):
            response = self.client.post("/api/search", json={
                "query": "A", "sources": ["pubmed"], "merge": True,
            })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "No run is loaded to refresh.")
        self.assertEqual(calls, [])


class RunsSearchLifecycleTests(RunsWebBase):
    def test_new_search_creates_a_run_folder_and_sets_it_active(self):
        job = self._search()

        self.assertEqual(job["status"], "done", job["log"])
        run_id = job["result"]["run_id"]
        self.assertTrue(run_id)
        self.assertFalse(job["result"]["merged"])
        self.assertEqual(job["result"]["added"], 1)
        self.assertEqual(runs.current_run_id(), run_id)

        folder = self.runs_dir / run_id
        self.assertTrue((folder / "search_results.xlsx").is_file())
        self.assertEqual(config.SEARCH_RESULTS_XLSX.parent, folder)

        state = runs.load_state(run_id)
        self.assertEqual(state["counts"]["papers"], 1)
        self.assertTrue(state["last_search_at"])
        self.assertEqual(state["query"]["rendered"], "A")
        self.assertEqual(state["sources"], ["pubmed"])
        self.assertEqual(state["settings"]["email"], "a@example.org")
        self.assertNotIn("api_key", json.dumps(state))

    def test_failed_new_search_restores_the_previous_run_and_deletes_the_new_one(self):
        previous = runs.create_run("Previous")
        runs.activate_run(previous)
        previous_sheet = config.SEARCH_RESULTS_XLSX
        previous_sheet.write_bytes(b"previous search workbook")

        job = self._search(
            search=mock.Mock(side_effect=RuntimeError("network down"))
        )

        self.assertEqual(job["status"], "error")
        self.assertIn("network down", job["error"])
        self.assertEqual(runs.current_run_id(), previous)
        self.assertEqual([item["id"] for item in runs.list_runs()], [previous])
        self.assertEqual(previous_sheet.read_bytes(), b"previous search workbook")
        self.assertEqual(config.SEARCH_RESULTS_XLSX, previous_sheet)
        self.assertEqual(
            sorted(path.name for path in self.runs_dir.iterdir()), [previous]
        )

    def test_cancelled_new_search_keeps_the_previous_dataset(self):
        previous = runs.create_run("Previous")
        runs.activate_run(previous)
        previous_sheet = config.SEARCH_RESULTS_XLSX
        previous_sheet.write_bytes(b"previous search workbook")

        job = self._job(cancel=True)
        job = self._search(job=job)

        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["result"]["cancelled"])
        self.assertEqual(runs.current_run_id(), previous)
        self.assertEqual([item["id"] for item in runs.list_runs()], [previous])
        self.assertEqual(previous_sheet.read_bytes(), b"previous search workbook")
        self.assertEqual(config.SEARCH_RESULTS_XLSX, previous_sheet)

    def test_refresh_merges_into_the_same_run_and_keeps_extraction_outputs(self):
        run_id = runs.create_run("Existing")
        runs.activate_run(run_id)
        folder = self.runs_dir / run_id

        web.writer.write_xlsx(
            config.SEARCH_RESULTS_XLSX, web.SEARCH_COLS,
            [web.paper_to_row(self._paper())],
        )
        make_workbook(
            config.EXTRACTED_XLSX,
            ["paper_key", "row_index", "mutation"],
            [["pmid:111", 1, "M1"]],
        )
        make_workbook(
            config.CLOSED_SOURCE_XLSX, ["title", "reason"],
            [["Closed Paper", "no PDF and no preprint found"]],
        )
        config.CHECKPOINT_JSON.write_text(
            json.dumps({"pmid:111": {"status": "extracted"}}), encoding="utf-8"
        )
        figure_index = config.FIGURES_INDEX_JSON
        figure_index.parent.mkdir(parents=True, exist_ok=True)
        figure_index.write_text(
            json.dumps({"version": 1, "figures": []}), encoding="utf-8"
        )
        extracted_before = config.EXTRACTED_XLSX.read_bytes()
        closed_before = config.CLOSED_SOURCE_XLSX.read_bytes()
        checkpoint_before = config.CHECKPOINT_JSON.read_bytes()
        figures_before = figure_index.read_bytes()

        new_paper = self._paper("Paper Two", "10.1/two", "222")
        job = self._search(papers=[new_paper], merge=True)

        self.assertEqual(job["status"], "done", job["log"])
        self.assertEqual(job["result"]["run_id"], run_id)
        self.assertTrue(job["result"]["merged"])
        self.assertEqual(job["result"]["added"], 1)
        self.assertEqual(job["result"]["total"], 2)
        self.assertEqual(runs.current_run_id(), run_id)
        self.assertEqual([item["id"] for item in runs.list_runs()], [run_id])

        rows = read_sheet(config.SEARCH_RESULTS_XLSX)
        titles = sorted(
            dict(zip(rows[0], row))["title"] for row in rows[1:]
        )
        self.assertEqual(titles, ["Paper One", "Paper Two"])

        self.assertEqual(config.EXTRACTED_XLSX.read_bytes(), extracted_before)
        self.assertEqual(config.CLOSED_SOURCE_XLSX.read_bytes(), closed_before)
        self.assertEqual(config.CHECKPOINT_JSON.read_bytes(), checkpoint_before)
        self.assertEqual(figure_index.read_bytes(), figures_before)
        self.assertTrue((folder / "pdfs").is_dir())

        state = runs.load_state(run_id)
        self.assertEqual(state["counts"]["papers"], 2)
        self.assertEqual(state["counts"]["extracted"], 1)
        self.assertEqual(state["counts"]["closed"], 1)
        self.assertTrue(state["last_search_at"])

    def test_query_selection_is_stored_and_malformed_paths_are_ignored(self):
        tree = {
            "type": "group", "op": "AND", "children": [
                {"type": "term", "text": "", "op": "AND"},
                {"type": "group", "op": "OR", "children": [
                    {"type": "term", "text": "B", "op": "AND"},
                ]},
            ],
        }
        selection = {
            "selected_paths": [[0], [1], ["bad"], [-1], [1, 0]],
            "children_selected_paths": [[1], "nope"],
        }
        job = self._search(query_tree=tree, query_selection=selection)
        self.assertEqual(job["status"], "done", job["log"])

        state = runs.load_state(job["result"]["run_id"])
        self.assertEqual(state["query"]["tree"], tree)
        self.assertEqual(state["query"]["selected_paths"], [[0], [1], [1, 0]])
        self.assertEqual(state["query"]["children_selected_paths"], [[1]])


class RunsExtractLifecycleTests(RunsWebBase):
    def test_extraction_updates_goal_columns_mode_and_counts(self):
        run_id = runs.create_run("Existing")
        runs.activate_run(run_id)
        web.writer.write_xlsx(
            config.SEARCH_RESULTS_XLSX, web.SEARCH_COLS,
            [web.paper_to_row(self._paper())],
        )
        paper = self._paper()
        job = self._job()

        with mock.patch.object(web, "load_settings", lambda: {}), \
             mock.patch.object(web, "apply_settings", lambda s: None), \
             mock.patch.object(
                 web, "_load_search_results", lambda required=False: [paper]
             ), \
             mock.patch.object(
                 web, "_prepare_paper",
                 lambda p: ("full text", "test", {
                     "path": None, "filename": "", "label": "none",
                     "reason": "no PDF",
                 }),
             ), \
             mock.patch.object(config, "PDF_ARCHIVE_ENABLED", False), \
             mock.patch.object(
                 web.extractor, "extract",
                 lambda text, fields=None, instruction=None:
                     [{"mutation": "M2"}],
             ):
            web._run_extract_job(job, "Find mutations", ["mutation"], "table")

        self.assertEqual(job["status"], "done", job["log"])
        state = runs.load_state(run_id)
        self.assertEqual(state["extraction"], {
            "goal": "Find mutations", "columns": ["mutation"], "mode": "table",
        })
        self.assertEqual(state["counts"]["papers"], 1)
        self.assertEqual(state["counts"]["extracted"], 1)
        self.assertEqual(state["counts"]["closed"], 0)
        self.assertTrue(state["last_extract_at"])
        self.assertEqual(state["status"], "done")

    def test_extraction_without_a_run_and_without_data_does_not_create_one(self):
        job = self._job()
        with mock.patch.object(web, "load_settings", lambda: {}), \
             mock.patch.object(web, "apply_settings", lambda s: None), \
             mock.patch.object(
                 web, "_load_search_results", lambda required=False: []
             ):
            web._run_extract_job(job, "Find mutations", [], "records")

        self.assertEqual(job["status"], "done")
        self.assertIn("No search results yet", "\n".join(job["log"]))
        self.assertEqual(runs.list_runs(), [])
        self.assertIsNone(runs.current_run_id())


class RunsMarkupTests(unittest.TestCase):
    def test_runs_card_and_selection_helpers_exist(self):
        root = Path(__file__).resolve().parent.parent
        template = (root / "templates" / "index.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="runs-card"', template)
        self.assertIn('id="run-select"', template)
        self.assertIn('id="run-load"', template)
        self.assertIn('id="run-refresh"', template)
        self.assertIn('id="run-delete"', template)
        self.assertIn("loadRuns", script)
        self.assertIn("applyRunState", script)
        self.assertIn("snapshotQuerySelection", script)
        self.assertIn("restoreQuerySelection", script)
        self.assertIn("pathForNode", script)
        self.assertIn("nodeAtPath", script)
        self.assertIn("query_selection", script)
        self.assertIn("merge", script)


if __name__ == "__main__":
    unittest.main()
