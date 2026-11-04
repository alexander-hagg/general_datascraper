"""Tests for the LLM extraction-goal suggester (mocked, no network)."""

import json
import unittest
from unittest import mock

import suggester


class GoalSuggesterTests(unittest.TestCase):
    def setUp(self):
        require = mock.patch.object(suggester, "_require_backend")
        self.require_backend = require.start()
        self.addCleanup(require.stop)
        call = mock.patch.object(suggester, "_call_llm")
        self.call_llm = call.start()
        self.addCleanup(call.stop)
        self.call_llm.return_value = json.dumps(
            {"suggestions": ["A clearer extraction goal."]}
        )

    def _prompt_and_schema(self):
        prompt, schema = self.call_llm.call_args.args
        return prompt, schema

    def test_goal_is_required_and_whitespace_only_is_rejected(self):
        for value in ("", "   ", "\n\t ", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    suggester.suggest_goal(value)
        self.call_llm.assert_not_called()

    def test_non_text_and_overlong_goals_are_rejected(self):
        with self.assertRaises(ValueError):
            suggester.suggest_goal(123)
        with self.assertRaises(ValueError):
            suggester.suggest_goal("Extract " + "x" * suggester.MAX_GOAL_CHARS)
        self.call_llm.assert_not_called()

    def test_invalid_mode_and_columns_are_rejected_before_the_llm(self):
        with self.assertRaises(ValueError):
            suggester.suggest_goal("Extract mutations", mode="merge")
        with self.assertRaises(ValueError):
            suggester.suggest_goal("Extract mutations", columns={"mutation": True})
        self.call_llm.assert_not_called()

    def test_prompt_contains_normalized_goal_columns_and_mode(self):
        result = suggester.suggest_goal(
            "  Every   mutation\tthat was tested ",
            columns=[" mutation ", "", "Effect", "MUTATION"],
            mode="TABLE",
        )
        self.assertEqual(result, ["A clearer extraction goal."])
        prompt, schema = self._prompt_and_schema()
        self.assertIn('Current goal: "Every mutation that was tested"', prompt)
        self.assertIn("Requested columns: mutation, Effect", prompt)
        self.assertIn("Extraction mode: table rows", prompt)
        for phrase in (
            "self-contained",
            "one record",
            "units",
            "negative",
            "Do not invent",
            "1-3",
        ):
            self.assertIn(phrase, prompt)
        self.assertEqual(schema["properties"]["suggestions"]["type"], "array")
        self.assertEqual(schema["required"], ["suggestions"])

    def test_comma_separated_columns_are_cleaned_and_mode_defaults_to_records(self):
        suggester.suggest_goal(
            "Extract mutations",
            columns=" mutation, , effect, Mutation ",
        )
        prompt, _ = self._prompt_and_schema()
        self.assertIn("Requested columns: mutation, effect", prompt)
        self.assertIn("Extraction mode: individual records", prompt)

    def test_columns_are_capped_at_fifty(self):
        columns = [f"column{i}" for i in range(60)]
        suggester.suggest_goal("Extract", columns=columns)
        prompt, _ = self._prompt_and_schema()
        self.assertIn("column49", prompt)
        self.assertNotIn("column50", prompt)

    def test_suggestions_are_cleaned_deduped_and_capped(self):
        self.call_llm.return_value = json.dumps({
            "suggestions": [
                " First goal. ",
                "- **Second**   goal. ",
                "1. First goal.",
                "Goal 2: Third goal",
                "",
                "Fourth goal",
            ]
        })
        self.assertEqual(
            suggester.suggest_goal("Extract mutations"),
            ["First goal.", "Second goal.", "Third goal"],
        )

    def test_per_item_length_is_capped(self):
        long_goal = "x" * (suggester.MAX_GOAL_SUGGESTION_CHARS + 100)
        self.call_llm.return_value = json.dumps({"suggestions": [long_goal]})
        result = suggester.suggest_goal("Extract mutations")
        self.assertEqual(len(result[0]), suggester.MAX_GOAL_SUGGESTION_CHARS)

    def test_fenced_json_and_empty_or_invalid_payloads(self):
        self.call_llm.return_value = (
            '```json\n{"suggestions": ["Fenced goal"]}\n```'
        )
        self.assertEqual(suggester.suggest_goal("Extract"), ["Fenced goal"])

        for raw in (
            '{"suggestions": []}',
            '{"suggestions": "not a list"}',
            '{"groups": []}',
            "not json at all",
        ):
            with self.subTest(raw=raw):
                self.call_llm.return_value = raw
                self.assertEqual(suggester.suggest_goal("Extract"), [])

    def test_backend_unavailable_propagates(self):
        self.require_backend.side_effect = suggester.BackendUnavailable("offline")
        with self.assertRaises(suggester.BackendUnavailable) as ctx:
            suggester.suggest_goal("Extract mutations")
        self.assertIn("offline", str(ctx.exception))
        self.call_llm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
