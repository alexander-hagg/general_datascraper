"""Tests for the global prompt history store (stdlib unittest, temp files)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import history


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "prompt_history.json"
        self._patches = [
            mock.patch.object(history.config, "PROMPT_HISTORY_JSON", self.path),
            mock.patch.object(history.config, "PROMPT_HISTORY_MAX_ENTRIES", 100),
            mock.patch.object(history.config, "PROMPT_HISTORY_MAX_PER_KIND", 50),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_missing_file_returns_an_empty_list(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(history.list_entries(), [])
        self.assertEqual(history.list_entries("search"), [])
        self.assertEqual(history.clear(), 0)
        self.assertFalse(self.path.exists())

    def test_corrupt_file_is_treated_as_empty(self):
        self.path.write_text("{definitely not json", encoding="utf-8")
        self.assertEqual(history.list_entries(), [])
        self.assertEqual(history.clear("goal"), 0)

        # A valid record replaces the corrupt document.
        entry = history.record("goal", {"text": "Find mutations"})
        self.assertEqual(entry["uses"], 1)
        self.assertEqual(history.list_entries("goal")[0]["data"]["text"], "Find mutations")
        self.assertEqual(self._read()["version"], 1)

    def test_wrong_document_shape_is_treated_as_empty(self):
        self.path.write_text(json.dumps({"version": 1, "entries": {}}), encoding="utf-8")
        self.assertEqual(history.list_entries(), [])
        self.path.write_text(json.dumps({"version": 2, "entries": []}), encoding="utf-8")
        self.assertEqual(history.list_entries(), [])

    def test_record_and_list_are_newest_used_first(self):
        first = history.record("goal", {"text": "First goal"})
        second = history.record("goal", {"text": "Second goal"})
        third = history.record("search", {"query": "A AND B"})

        self.assertEqual([e["id"] for e in history.list_entries()], [
            third["id"], second["id"], first["id"],
        ])
        self.assertEqual([e["id"] for e in history.list_entries("goal")], [
            second["id"], first["id"],
        ])
        for entry in history.list_entries():
            for key in ("id", "kind", "data", "created_at", "last_used", "uses"):
                self.assertIn(key, entry)
            self.assertEqual(entry["uses"], 1)
            self.assertIsInstance(entry["created_at"], str)
            self.assertIsInstance(entry["last_used"], str)

    def test_duplicate_updates_uses_and_moves_to_front(self):
        first = history.record("goal", {"text": "Same goal"})
        other = history.record("goal", {"text": "Other goal"})
        duplicate = history.record("goal", {"text": "Same goal"})

        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(duplicate["created_at"], first["created_at"])
        self.assertEqual(duplicate["uses"], 2)
        self.assertNotEqual(duplicate["last_used"], first["last_used"])

        entries = history.list_entries("goal")
        self.assertEqual([e["id"] for e in entries], [first["id"], other["id"]])
        self.assertEqual(entries[0]["uses"], 2)
        self.assertEqual(entries[1]["uses"], 1)

    def test_fingerprint_ignores_dict_key_order_and_query_tree_ids(self):
        tree = {
            "type": "group",
            "op": "AND",
            "children": [
                {"id": "n99", "type": "term", "text": "A", "op": "AND"},
                {
                    "id": "n100",
                    "type": "group",
                    "op": "NOT",
                    "children": [
                        {"id": "n101", "type": "term", "text": "C", "op": "AND"},
                    ],
                },
            ],
        }
        first = history.record("search", {
            "query": "A NOT C",
            "sources": ["pubmed"],
            "query_tree": tree,
        })
        # Different insertion order, different ids, same prompt.
        second = history.record("search", {
            "sources": ["pubmed"],
            "query_tree": {
                "children": [
                    {"text": "A", "type": "term", "op": "AND", "id": "x1"},
                    {
                        "children": [
                            {"type": "term", "op": "AND", "text": "C", "id": "x2"},
                        ],
                        "op": "NOT",
                        "type": "group",
                        "id": "x3",
                    },
                ],
                "op": "AND",
                "type": "group",
            },
            "query": "A NOT C",
        })

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["uses"], 2)
        self.assertEqual(len(history.list_entries("search")), 1)
        stored = history.list_entries("search")[0]["data"]["query_tree"]
        self.assertNotIn("id", stored)
        self.assertNotIn("id", stored["children"][0])
        self.assertNotIn("id", stored["children"][1])

    def test_per_kind_cap_keeps_most_recently_used(self):
        with mock.patch.object(history.config, "PROMPT_HISTORY_MAX_PER_KIND", 3):
            for index in range(5):
                history.record("goal", {"text": f"Goal {index}"})
            entries = history.list_entries("goal")
            self.assertEqual(len(entries), 3)
            self.assertEqual(
                [e["data"]["text"] for e in entries],
                ["Goal 4", "Goal 3", "Goal 2"],
            )

    def test_total_cap_keeps_most_recently_used_across_kinds(self):
        with mock.patch.object(history.config, "PROMPT_HISTORY_MAX_ENTRIES", 3):
            history.record("search", {"query": "One"})
            history.record("goal", {"text": "Two"})
            history.record("columns", {"text": "Three"})
            history.record("search", {"query": "Four"})

            entries = history.list_entries()
            self.assertEqual(len(entries), 3)
            self.assertEqual(
                [(e["kind"], e["data"].get("query") or e["data"].get("text")) for e in entries],
                [("search", "Four"), ("columns", "Three"), ("goal", "Two")],
            )

    def test_clear_one_kind_and_all(self):
        history.record("search", {"query": "A"})
        history.record("goal", {"text": "B"})
        history.record("columns", {"text": "C"})

        self.assertEqual(history.clear("goal"), 1)
        self.assertEqual(history.list_entries("goal"), [])
        self.assertEqual(len(history.list_entries()), 2)

        self.assertEqual(history.clear(), 2)
        self.assertEqual(history.list_entries(), [])
        self.assertEqual(self._read()["entries"], [])

    def test_invalid_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            history.record("other", {"text": "x"})
        with self.assertRaises(ValueError):
            history.list_entries("other")
        with self.assertRaises(ValueError):
            history.clear("other")
        self.assertFalse(self.path.exists())

    def test_non_serializable_data_is_rejected_without_writing(self):
        with self.assertRaises(ValueError) as raised:
            history.record("goal", {"text": object()})
        self.assertIn("JSON-serializable", str(raised.exception))
        self.assertFalse(self.path.exists())

        history.record("goal", {"text": "Valid"})
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            history.record("goal", {"text": float("nan")})
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_data_must_be_a_dict(self):
        with self.assertRaises(ValueError):
            history.record("goal", "not a dict")


if __name__ == "__main__":
    unittest.main()
