"""Run-folder storage, state and path-switching tests (no network, no Flask)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import runs


class RunsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.runs_dir = self.root / "runs"
        self.marker = self.root / "active_run.json"

        # Capture the real config paths before any run patch, so tearDown can
        # put the repository back exactly as it was even when a test fails.
        self._saved_paths = runs.capture_paths()
        self._patchers = [
            mock.patch.object(config, "RUNS_ENABLED", True),
            mock.patch.object(config, "RUNS_DIR", self.runs_dir),
            mock.patch.object(config, "ACTIVE_RUN_JSON", self.marker),
            mock.patch.object(config, "OUTPUT_DIR", self.output),
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

    def make_run(self, name="Test run", state=None):
        run_id = runs.create_run(name=name, state=state)
        self.assertIsNotNone(run_id)
        return run_id


class RunIdTests(RunsTestBase):
    def test_valid_ids_map_to_a_folder(self):
        for run_id in ("a", "20261103-142501-a1b2", "A.b_c-1"):
            self.assertEqual(runs.run_dir(run_id), self.runs_dir / run_id)

    def test_traversal_and_absolute_paths_are_rejected(self):
        for run_id in ("", "..", "../x", "/tmp/x", "a/b", "a\\b", ".hidden", None,
                       "a" * 81, "abc\n"):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ValueError):
                    runs.run_dir(run_id)

    def test_disabled_entry_points_do_not_touch_paths(self):
        with mock.patch.object(config, "RUNS_ENABLED", False):
            before = runs.capture_paths()
            self.assertIsNone(runs.run_dir("a"))
            self.assertIsNone(runs.create_run("A"))
            self.assertIsNone(runs.load_state("a"))
            self.assertIsNone(runs.current_run_id())
            self.assertIsNone(runs.ensure_active_run())
            self.assertIsNone(runs.apply_run_paths("a"))
            self.assertIsNone(runs.apply_legacy_paths())
            self.assertEqual(runs.list_runs(), [])
            self.assertFalse(runs.delete_run("a"))
            self.assertEqual(runs.capture_paths(), before)


class RunStorageTests(RunsTestBase):
    def test_create_list_load_update_delete(self):
        query_tree = {
            "type": "group",
            "op": "AND",
            "children": [
                {"type": "term", "text": "A", "op": "AND"},
                {"type": "group", "op": "OR", "children": []},
            ],
        }
        run_id = self.make_run(
            "Find mutations",
            {
                "status": "ready",
                "query": {
                    "tree": query_tree,
                    "rendered": "A",
                    "selected_paths": [[1]],
                    "children_selected_paths": [],
                },
                "sources": ["pubmed"],
                "max_results": 12,
                "counts": {"papers": 4},
                "settings": {"email": "a@example.org", "model": "m"},
            },
        )

        folder = self.runs_dir / run_id
        self.assertTrue((folder / "state.json").is_file())
        for name in ("pdfs", "annotated_pdfs", "figures"):
            self.assertTrue((folder / name).is_dir(), name)

        state = runs.load_state(run_id)
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["id"], run_id)
        self.assertEqual(state["name"], "Find mutations")
        self.assertEqual(state["query"]["tree"], query_tree)
        self.assertEqual(state["query"]["selected_paths"], [[1]])
        self.assertEqual(state["sources"], ["pubmed"])
        self.assertEqual(state["max_results"], 12)

        listed = runs.list_runs()
        self.assertEqual([item["id"] for item in listed], [run_id])
        self.assertEqual(listed[0]["counts"]["papers"], 4)

        updated = runs.update_state(
            run_id,
            status="extracting",
            counts={"extracted": 3},
            extraction={"goal": "Find", "columns": ["mutation"], "mode": "table"},
        )
        self.assertEqual(updated["status"], "extracting")
        self.assertEqual(updated["counts"]["papers"], 4)
        self.assertEqual(updated["counts"]["extracted"], 3)
        self.assertEqual(updated["extraction"]["columns"], ["mutation"])
        self.assertEqual(updated["extraction"]["mode"], "table")

        replacement = dict(updated)
        replacement["name"] = "Renamed"
        saved = runs.save_state(run_id, replacement)
        self.assertEqual(saved["name"], "Renamed")
        self.assertEqual(runs.load_state(run_id)["name"], "Renamed")

        self.assertTrue(runs.delete_run(run_id))
        self.assertFalse(folder.exists())
        self.assertFalse(runs.delete_run(run_id))

    def test_state_round_trip_keeps_selection_paths(self):
        run_id = self.make_run(
            "Selection",
            {
                "query": {
                    "tree": {"type": "group", "op": "AND", "children": [
                        {"type": "term", "text": "", "op": "AND"},
                        {"type": "group", "op": "OR", "children": [
                            {"type": "term", "text": "B", "op": "AND"},
                            {"type": "term", "text": "C", "op": "OR"},
                        ]},
                    ]},
                    "rendered": "B OR C",
                    "selected_paths": [[0], [1, 0]],
                    "children_selected_paths": [[1]],
                },
            },
        )
        state = runs.load_state(run_id)
        self.assertEqual(state["query"]["selected_paths"], [[0], [1, 0]])
        self.assertEqual(state["query"]["children_selected_paths"], [[1]])
        # Empty terms survive a save/load round trip so those paths stay stable.
        self.assertEqual(state["query"]["tree"]["children"][0]["text"], "")

    def test_malformed_selection_paths_are_ignored(self):
        cleaned = runs.sanitize_query_selection({
            "selected_paths": [[0], [0], [-1], ["x"], [True], [1, 2], None],
            "children_selected_paths": "not a list",
        })
        self.assertEqual(cleaned, {
            "selected_paths": [[0], [1, 2]],
            "children_selected_paths": [],
        })

    def test_secrets_and_output_paths_never_reach_state(self):
        run_id = self.make_run(
            "Secrets",
            {
                "settings": {
                    "email": "a@example.org",
                    "model": "m",
                    "api_key": "TOP-SECRET",
                    "openai_base_url": "https://secret.example/v1",
                },
                "output_path": "/absolute/output/search_results.xlsx",
                "search_results_xlsx": "/absolute/output/search_results.xlsx",
            },
        )
        state = runs.load_state(run_id)
        self.assertEqual(set(state["settings"]), {"email", "model"})
        serialized = json.dumps(state)
        self.assertNotIn("TOP-SECRET", serialized)
        self.assertNotIn("secret.example", serialized)
        self.assertNotIn("/absolute/output", serialized)

        metadata = runs.list_runs()[0]
        self.assertEqual(
            set(metadata),
            {
                "id", "name", "created_at", "updated_at", "status", "counts",
                "query", "rendered_query", "last_search_at", "last_extract_at",
            },
        )
        metadata_text = json.dumps(metadata)
        self.assertNotIn("a@example.org", metadata_text)
        self.assertNotIn(str(self.runs_dir), metadata_text)
        self.assertNotIn("TOP-SECRET", metadata_text)

    def test_corrupt_or_missing_state_is_skipped(self):
        broken = self.make_run("Broken")
        (self.runs_dir / broken / "state.json").write_text("{not json", encoding="utf-8")
        missing = self.make_run("Missing")
        (self.runs_dir / missing / "state.json").unlink()
        valid = self.make_run("Valid")

        self.assertIsNone(runs.load_state(broken))
        self.assertIsNone(runs.load_state(missing))
        self.assertEqual([item["id"] for item in runs.list_runs()], [valid])


class ActiveRunTests(RunsTestBase):
    def test_active_marker_round_trip_and_corruption(self):
        run_id = self.make_run()
        self.assertEqual(runs.set_active(run_id), run_id)
        marker = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(marker["run_id"], run_id)
        self.assertEqual(runs.current_run_id(), run_id)

        self.marker.write_text("{broken", encoding="utf-8")
        self.assertIsNone(runs.current_run_id())

        self.marker.write_text(json.dumps({"run_id": "../escape"}), encoding="utf-8")
        self.assertIsNone(runs.current_run_id())

        self.marker.write_text(
            json.dumps({"run_id": "20200101-000000-nope"}), encoding="utf-8"
        )
        self.assertIsNone(runs.current_run_id())

        # A marker pointing at a run whose state.json is corrupt is not valid.
        (self.runs_dir / run_id / "state.json").write_text("{broken", encoding="utf-8")
        self.marker.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        self.assertIsNone(runs.current_run_id())

    def test_ensure_active_run_picks_the_newest_valid_run(self):
        older = self.make_run("Older")
        newer = self.make_run("Newer")

        def stamp(run_id, value):
            state = runs.load_state(run_id)
            state["updated_at"] = value
            runs.save_state(run_id, state)

        stamp(older, "2020-01-01T00:00:00+00:00")
        stamp(newer, "2021-01-01T00:00:00+00:00")

        self.assertIsNone(runs.current_run_id())
        self.assertEqual(runs.ensure_active_run(), newer)
        self.assertEqual(runs.current_run_id(), newer)
        self.assertEqual(config.SEARCH_RESULTS_XLSX.parent, self.runs_dir / newer)

    def test_ensure_active_run_ignores_corrupt_newest_and_uses_valid_run(self):
        broken = self.make_run("Broken")
        (self.runs_dir / broken / "state.json").write_text("{", encoding="utf-8")
        valid = self.make_run("Valid")
        self.assertEqual(runs.ensure_active_run(), valid)

    def test_path_switching_and_legacy_restore(self):
        original_prompt_history = config.PROMPT_HISTORY_JSON
        original_output = config.OUTPUT_DIR
        run_id = self.make_run()
        folder = runs.apply_run_paths(run_id)

        self.assertEqual(config.ACTIVE_RUN_DIR, folder)
        self.assertEqual(config.SEARCH_RESULTS_XLSX, folder / "search_results.xlsx")
        self.assertEqual(config.CLOSED_SOURCE_XLSX, folder / "closed_source_papers.xlsx")
        self.assertEqual(config.EXTRACTED_XLSX, folder / "extracted_data.xlsx")
        self.assertEqual(config.CHECKPOINT_JSON, folder / "checkpoint.json")
        self.assertEqual(config.LOCAL_PDF_INDEX_JSON, folder / "local_pdfs.json")
        self.assertEqual(config.PDF_INDEX_JSON, folder / "pdf_index.json")
        self.assertEqual(config.ANNOTATED_PDF_DIR, folder / "annotated_pdfs")
        self.assertEqual(config.PDF_DIR, folder / "pdfs")
        self.assertEqual(config.FIGURES_DIR, folder / "figures")
        self.assertEqual(config.FIGURES_INDEX_JSON, folder / "figures" / "index.json")
        self.assertEqual(config.FIGURES_XLSX, folder / "figures.xlsx")
        # Global settings/history are intentionally untouched.
        self.assertEqual(config.OUTPUT_DIR, original_output)
        self.assertEqual(config.PROMPT_HISTORY_JSON, original_prompt_history)

        runs.apply_legacy_paths()
        self.assertIsNone(config.ACTIVE_RUN_DIR)
        self.assertEqual(config.SEARCH_RESULTS_XLSX, self.output / "search_results.xlsx")
        self.assertEqual(config.EXTRACTED_XLSX, self.output / "extracted_data.xlsx")
        self.assertEqual(config.FIGURES_INDEX_JSON, self.output / "figures" / "index.json")
        self.assertEqual(config.OUTPUT_DIR, original_output)
        self.assertEqual(config.PROMPT_HISTORY_JSON, original_prompt_history)

    def test_capture_and_restore_rolls_paths_back(self):
        snapshot = runs.capture_paths()
        run_id = self.make_run()
        runs.apply_run_paths(run_id)
        self.assertNotEqual(config.SEARCH_RESULTS_XLSX, snapshot["SEARCH_RESULTS_XLSX"])
        runs.restore_paths(snapshot)
        self.assertEqual(config.SEARCH_RESULTS_XLSX, snapshot["SEARCH_RESULTS_XLSX"])
        self.assertEqual(config.ACTIVE_RUN_DIR, snapshot["ACTIVE_RUN_DIR"])

    def test_delete_active_run_clears_marker_and_restores_legacy_paths(self):
        run_id = self.make_run()
        runs.activate_run(run_id)
        self.assertEqual(config.SEARCH_RESULTS_XLSX.parent, self.runs_dir / run_id)

        self.assertTrue(runs.delete_run(run_id))
        self.assertFalse(self.marker.exists())
        self.assertIsNone(runs.current_run_id())
        self.assertIsNone(config.ACTIVE_RUN_DIR)
        self.assertEqual(config.SEARCH_RESULTS_XLSX, self.output / "search_results.xlsx")

    def test_delete_non_active_run_keeps_the_active_marker(self):
        active = self.make_run("Active")
        other = self.make_run("Other")
        runs.activate_run(active)
        self.assertTrue(runs.delete_run(other))
        self.assertEqual(runs.current_run_id(), active)


class LegacyMigrationTests(RunsTestBase):
    def _write_legacy(self):
        (self.output / "search_results.xlsx").write_bytes(b"search")
        (self.output / "extracted_data.xlsx").write_bytes(b"extracted")
        (self.output / "closed_source_papers.xlsx").write_bytes(b"closed")
        (self.output / "checkpoint.json").write_text('{"pmid:1": {}}', encoding="utf-8")
        (self.output / "local_pdfs.json").write_text('{"pmid:1": {}}', encoding="utf-8")
        (self.output / "pdf_index.json").write_text('{"pmid:1": {}}', encoding="utf-8")
        (self.output / "figures.xlsx").write_bytes(b"figures")
        (self.output / "pdfs").mkdir()
        (self.output / "pdfs" / "PMID_1.pdf").write_bytes(b"%PDF-1.4")
        (self.output / "annotated_pdfs").mkdir()
        (self.output / "annotated_pdfs" / "PMID_1_ann.pdf").write_bytes(b"%PDF-1.4")
        (self.output / "figures").mkdir()
        (self.output / "figures" / "index.json").write_text(
            '{"version": 1, "figures": []}', encoding="utf-8"
        )
        (self.output / "web_settings.json").write_text('{"email": "private"}', encoding="utf-8")
        (self.output / "prompt_history.json").write_text('{"version": 1}', encoding="utf-8")

    def test_legacy_artifacts_are_copied_and_source_is_untouched(self):
        self._write_legacy()
        before = {
            path.name: path.read_bytes()
            for path in self.output.iterdir()
            if path.is_file()
        }

        run_id = runs.ensure_active_run()
        self.assertIsNotNone(run_id)
        state = runs.load_state(run_id)
        self.assertEqual(state["name"], "Imported run")
        folder = self.runs_dir / run_id

        for name in (
            "search_results.xlsx", "extracted_data.xlsx",
            "closed_source_papers.xlsx", "checkpoint.json",
            "local_pdfs.json", "pdf_index.json", "figures.xlsx",
        ):
            self.assertEqual(
                (folder / name).read_bytes(), (self.output / name).read_bytes(), name
            )
        self.assertEqual(
            (folder / "pdfs" / "PMID_1.pdf").read_bytes(), b"%PDF-1.4"
        )
        self.assertTrue((folder / "annotated_pdfs" / "PMID_1_ann.pdf").is_file())
        self.assertTrue((folder / "figures" / "index.json").is_file())

        # Global files/settings/history are only ever copied, never moved or
        # deleted, and non-dataset files are not imported.
        for name, content in before.items():
            self.assertEqual((self.output / name).read_bytes(), content, name)
        self.assertFalse((folder / "web_settings.json").exists())
        self.assertFalse((folder / "prompt_history.json").exists())
        self.assertFalse((folder / "runs").exists())
        self.assertTrue((self.output / "web_settings.json").exists())
        self.assertTrue((self.output / "prompt_history.json").exists())

        self.assertEqual(runs.current_run_id(), run_id)
        self.assertEqual(config.SEARCH_RESULTS_XLSX.parent, folder)

    def test_no_artifacts_leaves_no_active_run(self):
        self.assertIsNone(runs.ensure_active_run())
        self.assertIsNone(runs.current_run_id())
        self.assertFalse(self.marker.exists())
        self.assertEqual(runs.list_runs(), [])
        self.assertEqual(config.SEARCH_RESULTS_XLSX, self.output / "search_results.xlsx")


class MetadataTests(RunsTestBase):
    def test_newest_updated_run_is_first(self):
        first = self.make_run("First")
        second = self.make_run("Second")
        for run_id, stamp in (
            (first, "2022-01-01T00:00:00+00:00"),
            (second, "2022-02-01T00:00:00+00:00"),
        ):
            state = runs.load_state(run_id)
            state["updated_at"] = stamp
            runs.save_state(run_id, state)
        self.assertEqual([item["id"] for item in runs.list_runs()], [second, first])

    def test_query_rendered_text_is_included_but_not_the_tree(self):
        run_id = self.make_run(
            "Query",
            {"query": {"rendered": "A AND B", "tree": {
                "type": "group", "op": "AND", "children": [],
            }}},
        )
        metadata = runs.list_runs()[0]
        self.assertEqual(metadata["rendered_query"], "A AND B")
        self.assertEqual(metadata["query"], "A AND B")
        self.assertNotIn("tree", metadata)
        self.assertNotIn("query", metadata["counts"])
        self.assertEqual(runs.load_state(run_id)["id"], run_id)


if __name__ == "__main__":
    unittest.main()
