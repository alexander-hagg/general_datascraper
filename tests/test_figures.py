"""Tests for PDF figure detection, indexing, serving and gallery integration."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf
from openpyxl import load_workbook

import config
import figures
import main
import app as web


def make_pdf(path, pages):
    """Create a real PDF; `pages` is a list of callables taking the page."""
    doc = pymupdf.open()
    for build in pages:
        page = doc.new_page()
        build(page)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def caption_page(caption="Figure 1. Dose response of the mutant.", y=400,
                 draw_rect=(72, 150, 400, 370)):
    def build(page):
        if draw_rect:
            rect = pymupdf.Rect(*draw_rect)
            page.draw_rect(rect, color=(0, 0, 1), width=2)
            # A second vector component makes the union/selection path run.
            page.draw_line(
                (rect.x0 + 10, rect.y1 - 10),
                (rect.x1 - 10, rect.y0 + 10),
            )
        page.insert_text((72, y), caption)
    return build


def png_image_bytes(width=300, height=200, shade=180):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pix.clear_with(shade)
    return pix.tobytes("png")


def read_sheet(path):
    wb = load_workbook(path, read_only=True)
    ws = wb.active
    rows = [list(row) for row in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


class FigureTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.figures_dir = self.dir / "figures"
        self.index_path = self.figures_dir / "index.json"
        self.xlsx_path = self.dir / "figures.xlsx"
        patches = [
            mock.patch.object(config, "FIGURES_ENABLED", True),
            mock.patch.object(config, "FIGURES_GOAL_FILTER", True),
            mock.patch.object(config, "FIGURE_DPI", 100),
            mock.patch.object(config, "FIGURE_MAX_PER_PAPER", 50),
            mock.patch.object(config, "FIGURE_MAX_CANDIDATES", 100),
            mock.patch.object(config, "FIGURES_DIR", self.figures_dir),
            mock.patch.object(config, "FIGURES_INDEX_JSON", self.index_path),
            mock.patch.object(config, "FIGURES_XLSX", self.xlsx_path),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def paper_pdf(self, name="paper.pdf", caption="Figure 1. Dose response."):
        return make_pdf(
            self.dir / "pdfs" / name,
            [caption_page(caption=caption)],
        )


class DetectionTests(FigureTestBase):
    def test_caption_page_saves_png_with_metadata(self):
        pdf = self.paper_pdf()
        entries = figures.detect_figures(
            pdf, "pmid:123", title="A paper title"
        )
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["paper_key"], "pmid:123")
        self.assertEqual(entry["title"], "A paper title")
        self.assertEqual(entry["label"], "Figure 1")
        self.assertEqual(entry["page"], 1)
        self.assertEqual(entry["kind"], "caption")
        self.assertIn("Dose response", entry["caption"])
        self.assertEqual(entry["dpi"], 100)
        self.assertGreater(entry["width"], 0)
        self.assertGreater(entry["height"], 0)
        self.assertEqual(entry["file"], f"pmid_123/fig_{entry['id']}.png")
        self.assertFalse(Path(entry["file"]).is_absolute())
        png = self.figures_dir / entry["file"]
        self.assertTrue(png.is_file())
        self.assertTrue(png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        # The caption crop is smaller than the full page, not a page fallback.
        self.assertLess(entry["height"], 800)

    def test_table_caption_produces_a_caption_crop(self):
        def build(page):
            page.insert_text((72, 100), "Table 2A. Kinetic parameters.")
            page.draw_rect(pymupdf.Rect(72, 120, 500, 320), color=(0, 0, 0))

        pdf = make_pdf(self.dir / "table.pdf", [build])
        entries = figures.detect_figures(pdf, "doi:10.1/table")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"], "Table 2A")
        self.assertEqual(entries[0]["kind"], "caption")
        self.assertIn("Kinetic parameters", entries[0]["caption"])

    def test_too_small_caption_crop_falls_back_to_full_page(self):
        def build(page):
            page.insert_text((4, 35), "Figure 1. x")

        doc = pymupdf.open()
        page = doc.new_page(width=72, height=72)
        build(page)
        pdf = self.dir / "tiny.pdf"
        doc.save(pdf)
        doc.close()

        entries = figures.detect_figures(pdf, "pmid:tiny")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "page")
        self.assertEqual(entries[0]["label"], "Figure 1")

    def test_large_embedded_image_is_found_without_a_caption(self):
        def build(page):
            page.insert_image(
                pymupdf.Rect(20, 20, 280, 220),
                stream=png_image_bytes(300, 200),
            )

        pdf = make_pdf(self.dir / "image.pdf", [build])
        entries = figures.detect_figures(pdf, "pmid:img")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "image")
        self.assertEqual(entries[0]["label"], "Image p.1")
        self.assertEqual(entries[0]["page"], 1)

    def test_small_icons_are_not_saved(self):
        def build(page):
            page.insert_image(
                pymupdf.Rect(20, 20, 50, 50),
                stream=png_image_bytes(40, 40),
            )
            page.insert_text((72, 100), "No caption here.")

        pdf = make_pdf(self.dir / "icons.pdf", [build])
        self.assertEqual(figures.detect_figures(pdf, "pmid:icons"), [])

    def test_repeated_detection_reuses_files_and_counts_duplicates(self):
        pdf = self.paper_pdf()
        first = figures.process_paper(pdf, "pmid:dup", title="Dup")
        second = figures.process_paper(pdf, "pmid:dup", title="Dup")
        self.assertEqual(first["saved"], 1)
        self.assertEqual(second["saved"], 1)
        self.assertGreaterEqual(second["duplicates"], 1)
        self.assertEqual(
            [entry["id"] for entry in figures.load_index()],
            [entry["id"] for entry in figures.load_index()],
        )
        self.assertEqual(len(figures.load_index()), 1)

    def test_per_paper_cap_reports_capped(self):
        pdf = make_pdf(
            self.dir / "many.pdf",
            [
                caption_page("Figure 1. First."),
                caption_page("Figure 2. Second."),
            ],
        )
        with mock.patch.object(config, "FIGURE_MAX_PER_PAPER", 1):
            summary = figures.process_paper(pdf, "pmid:cap")
        self.assertEqual(summary["saved"], 1)
        self.assertTrue(summary["capped"])
        self.assertIn("capped", figures.summary_message(summary))

    def test_disabled_and_missing_pdf_do_not_raise(self):
        pdf = self.paper_pdf()
        with mock.patch.object(config, "FIGURES_ENABLED", False):
            summary = figures.process_paper(pdf, "pmid:disabled")
        self.assertEqual(summary["saved"], 0)
        self.assertIn("disabled", summary["warning"])

        missing = self.dir / "missing.pdf"
        summary = figures.process_paper(missing, "pmid:missing")
        self.assertEqual(summary["saved"], 0)
        self.assertIn("not found", summary["warning"])

    def test_missing_pymupdf_is_reported(self):
        pdf = self.paper_pdf()
        with mock.patch.object(figures, "PYMUPDF_AVAILABLE", False), \
             mock.patch.object(figures, "pymupdf", None):
            summary = figures.process_paper(pdf, "pmid:nopymupdf")
        self.assertEqual(summary["saved"], 0)
        self.assertIn("PyMuPDF", summary["warning"])


def candidate(candidate_id, label="Figure 1", page=1, kind="caption",
              caption="Dose response of the mutant."):
    return {
        "id": candidate_id,
        "page_number": page,
        "kind": kind,
        "label": label,
        "caption": caption,
    }


class GoalSelectionUnitTests(unittest.TestCase):
    def _select(self, raw, goal="Find figures showing current traces",
                title="A paper title", candidates=None):
        candidates = candidates or [
            candidate("c1", "Figure 1"),
            candidate("c2", "Figure 2", page=2),
        ]
        with mock.patch.object(figures.suggester, "_require_backend"), \
             mock.patch.object(
                 figures.suggester, "_call_llm", return_value=raw
             ) as call_llm:
            result = figures.select_figures_for_goal(
                candidates, goal, title=title
            )
        return result, call_llm

    def test_selects_only_known_ids_and_dedupes(self):
        result, _ = self._select(json.dumps({"selected": [
            {"id": "c2", "reason": "Shows the current traces."},
            {"id": "missing", "reason": "Invented id."},
            {"id": "c2", "reason": "Duplicate id."},
        ]}))
        self.assertEqual(
            [item["id"] for item in result["selected"]], ["c2"]
        )
        self.assertEqual(
            result["selected"][0]["reason"], "Shows the current traces."
        )
        self.assertIn("unknown figure id", result["warning"])
        self.assertIn("duplicate figure id", result["warning"])
        self.assertFalse(result["capped"])

    def test_malformed_responses_are_warnings(self):
        for raw in ("not json", '{"selected": "c1"}', "[]"):
            with self.subTest(raw=raw):
                result, _ = self._select(raw)
                self.assertEqual(result["selected"], [])
                self.assertTrue(result["warning"])

    def test_backend_unavailable_and_llm_exception_are_warnings(self):
        candidates = [candidate("c1")]
        with mock.patch.object(
            figures.suggester,
            "_require_backend",
            side_effect=figures.suggester.BackendUnavailable("offline"),
        ):
            result = figures.select_figures_for_goal(
                candidates, "Find current traces"
            )
        self.assertEqual(result["selected"], [])
        self.assertIn("backend is unavailable", result["warning"])

        with mock.patch.object(figures.suggester, "_require_backend"), \
             mock.patch.object(
                 figures.suggester,
                 "_call_llm",
                 side_effect=RuntimeError("model exploded"),
             ):
            result = figures.select_figures_for_goal(
                candidates, "Find current traces"
            )
        self.assertEqual(result["selected"], [])
        self.assertIn("Figure selection failed", result["warning"])

    def test_empty_goal_does_not_call_the_backend(self):
        with mock.patch.object(
            figures.suggester,
            "_require_backend",
            side_effect=AssertionError("backend must not be called"),
        ):
            result = figures.select_figures_for_goal(
                [candidate("c1")], "   "
            )
        self.assertTrue(result["goal_empty"])
        self.assertEqual(result["selected"], [])
        self.assertIn("goal is empty", result["warning"])

    def test_selection_is_capped_at_max_figures(self):
        candidates = [candidate(f"c{i}", label=f"Figure {i}") for i in range(1, 4)]
        with mock.patch.object(config, "FIGURE_MAX_PER_PAPER", 2):
            result, _ = self._select(
                json.dumps({"selected": [
                    {"id": "c1", "reason": "one"},
                    {"id": "c2", "reason": "two"},
                    {"id": "c3", "reason": "three"},
                ]}),
                candidates=candidates,
            )
        self.assertEqual([item["id"] for item in result["selected"]], ["c1", "c2"])
        self.assertTrue(result["capped"])

    def test_prompt_and_schema_include_goal_title_and_candidate_metadata(self):
        raw = json.dumps({"selected": []})
        result, call_llm = self._select(
            raw,
            goal="Find figures showing current traces for each mutation",
            title="Mutant channel paper",
        )
        self.assertEqual(result["selected"], [])
        prompt, schema = call_llm.call_args.args
        self.assertIn("current traces for each mutation", prompt)
        self.assertIn("Mutant channel paper", prompt)
        self.assertIn("Figure 2", prompt)
        self.assertIn("page: 2", prompt)
        self.assertIn("kind: caption", prompt)
        self.assertIn("Dose response of the mutant", prompt)
        self.assertIn("Do not select figures merely because they exist", prompt)
        self.assertIn("return an empty list", prompt)
        self.assertEqual(schema["required"], ["selected"])


class GoalFilteredProcessTests(FigureTestBase):
    def _three_figure_pdf(self):
        return make_pdf(
            self.dir / "goal.pdf",
            [
                caption_page("Figure 1. First current trace."),
                caption_page("Figure 2. Second current trace."),
                caption_page("Figure 3. Third current trace."),
            ],
        )

    def _process(self, pdf, goal, raw, **kwargs):
        with mock.patch.object(figures.suggester, "_require_backend"), \
             mock.patch.object(
                 figures.suggester, "_call_llm", return_value=raw
             ) as call_llm:
            summary = figures.process_paper(
                pdf, "pmid:goal", title="Goal paper", goal=goal, **kwargs
            )
        return summary, call_llm

    def test_only_selected_candidates_are_rendered_indexed_and_written(self):
        pdf = self._three_figure_pdf()
        summary, _ = self._process(
            pdf,
            "Find figures showing current traces for each mutation",
            json.dumps({"selected": [
                {"id": "c2", "reason": "Shows current traces for the mutation."}
            ]}),
        )
        self.assertEqual(summary["candidates"], 3)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["skipped"], 2)
        self.assertTrue(summary["goal_filtered"])
        self.assertEqual(summary["saved"], 1)

        entries = figures.load_index()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"], "Figure 2")
        self.assertEqual(
            entries[0]["reason"], "Shows current traces for the mutation."
        )
        self.assertEqual(
            figures.api_list("pmid:goal")[0]["reason"],
            "Shows current traces for the mutation.",
        )
        pngs = list(self.figures_dir.rglob("*.png"))
        self.assertEqual(len(pngs), 1)

        table = read_sheet(self.xlsx_path)
        row = dict(zip(table[0], table[1]))
        self.assertEqual(row["reason"], "Shows current traces for the mutation.")
        self.assertIn(
            "1 figure screenshot(s) matched the extraction goal "
            "(from 3 candidate(s))",
            figures.summary_message(summary),
        )

    def test_goal_without_a_figure_request_saves_none(self):
        pdf = self._three_figure_pdf()
        summary, _ = self._process(
            pdf, "Extract mutation effects", json.dumps({"selected": []})
        )
        self.assertEqual(summary["saved"], 0)
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(summary["skipped"], 3)
        self.assertEqual(figures.load_index(), [])
        self.assertEqual(list(self.figures_dir.rglob("*.png")), [])
        self.assertEqual(
            figures.summary_message(summary),
            "No figures matched the extraction goal (3 candidate(s) skipped).",
        )

    def test_empty_goal_saves_none_without_the_backend_and_clears_old_entries(self):
        pdf = self._three_figure_pdf()
        figures.process_paper(pdf, "pmid:goal")
        self.assertEqual(len(figures.load_index()), 3)
        with mock.patch.object(
            figures.suggester,
            "_require_backend",
            side_effect=AssertionError("backend must not be called"),
        ), mock.patch.object(
            figures.suggester,
            "_call_llm",
            side_effect=AssertionError("LLM must not be called"),
        ):
            summary = figures.process_paper(
                pdf, "pmid:goal", goal="   "
            )
        self.assertEqual(summary["saved"], 0)
        self.assertTrue(summary["goal_empty"])
        self.assertIn("goal is empty", figures.summary_message(summary))
        self.assertEqual(figures.load_index(), [])
        table = read_sheet(self.xlsx_path)
        self.assertEqual(table, [figures.FIGURE_COLUMNS])

    def test_goal_none_preserves_legacy_all_figures(self):
        pdf = self._three_figure_pdf()
        with mock.patch.object(
            figures.suggester,
            "_require_backend",
            side_effect=AssertionError("legacy mode must not call the LLM"),
        ):
            summary = figures.process_paper(pdf, "pmid:goal")
        self.assertEqual(summary["saved"], 3)
        self.assertFalse(summary["goal_filtered"])
        self.assertTrue(
            all(entry["reason"] == "" for entry in figures.load_index())
        )
        self.assertIn(
            "3 figure screenshot(s) saved", figures.summary_message(summary)
        )

    def test_goal_filter_disabled_selects_all_figures(self):
        pdf = self._three_figure_pdf()
        with mock.patch.object(config, "FIGURES_GOAL_FILTER", False), \
             mock.patch.object(
                 figures.suggester,
                 "_require_backend",
                 side_effect=AssertionError("disabled filter must not call the LLM"),
             ):
            summary = figures.process_paper(
                pdf, "pmid:goal", goal="Find figures showing current traces"
            )
        self.assertEqual(summary["saved"], 3)
        self.assertFalse(summary["goal_filtered"])
        self.assertEqual(len(figures.load_index()), 3)

    def test_candidate_cap_limits_the_prompt(self):
        pdf = self._three_figure_pdf()
        with mock.patch.object(config, "FIGURE_MAX_CANDIDATES", 2):
            summary, call_llm = self._process(
                pdf,
                "Find current traces",
                json.dumps({"selected": [
                    {"id": "c1", "reason": "First trace."}
                ]}),
            )
            message = figures.summary_message(summary)
        prompt, _schema = call_llm.call_args.args
        self.assertIn("id: c1", prompt)
        self.assertIn("id: c2", prompt)
        self.assertNotIn("id: c3", prompt)
        self.assertEqual(summary["candidates"], 2)
        self.assertTrue(summary["candidate_capped"])
        self.assertIn("candidate list capped at 2", message)

    def test_backend_failure_is_a_warning_not_an_exception(self):
        pdf = self._three_figure_pdf()
        with mock.patch.object(
            figures.suggester,
            "_require_backend",
            side_effect=figures.suggester.BackendUnavailable("offline"),
        ):
            summary = figures.process_paper(
                pdf, "pmid:goal", goal="Find current traces"
            )
        self.assertEqual(summary["saved"], 0)
        self.assertIn("backend is unavailable", summary["warning"])
        self.assertTrue(figures.summary_message(summary).startswith("Figure warning:"))


class IndexTests(FigureTestBase):
    def _entry(self, figure_id="abc12345", paper_key="pmid:1",
               relative="pmid_1/fig_abc12345.png", page=1, label="Figure 1"):
        return {
            "id": figure_id,
            "paper_key": paper_key,
            "title": "Paper",
            "label": label,
            "caption": "Caption",
            "page": page,
            "file": relative,
            "kind": "caption",
            "width": 10,
            "height": 20,
            "dpi": 150,
            "created_at": "2026-01-01T00:00:00Z",
        }

    def _write_png(self, relative):
        path = self.figures_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return path

    def test_index_round_trip_and_shape(self):
        self._write_png("pmid_2/fig_b.png")
        self._write_png("pmid_1/fig_a.png")
        entries = [
            self._entry("bbbbbbbb", "pmid:2", "pmid_2/fig_b.png", page=2),
            self._entry("aaaaaaaa", "pmid:1", "pmid_1/fig_a.png", page=1),
        ]
        figures.save_index(entries)
        data = json.loads(self.index_path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["figures"]), 2)
        loaded = figures.load_index()
        self.assertEqual([entry["id"] for entry in loaded], ["aaaaaaaa", "bbbbbbbb"])
        self.assertEqual(loaded[0]["file"], "pmid_1/fig_a.png")

    def test_missing_and_corrupt_index_return_empty(self):
        self.assertEqual(figures.load_index(), [])
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(figures.load_index(), [])
        self.index_path.write_text('{"version": 1, "figures": "nope"}', encoding="utf-8")
        self.assertEqual(figures.load_index(), [])

    def test_unsafe_relative_paths_are_rejected(self):
        self.assertEqual(figures._safe_relative("../secret.png"), "")
        self.assertEqual(figures._safe_relative("/etc/passwd"), "")
        self.assertEqual(figures._safe_relative("C:\\secret.png"), "")
        self.assertEqual(figures._safe_relative("a/../../b.png"), "")

        figures.save_index([self._entry(relative="../secret.png")])
        self.assertEqual(figures.load_index(), [])

        # A hand-written index entry with traversal metadata is ignored even
        # before load_index sanitizes it.
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps({
            "version": 1,
            "figures": [self._entry(relative="../secret.png")],
        }), encoding="utf-8")
        self.assertIsNone(figures.resolve_figure_path("abc12345"))

    def test_paths_cannot_escape_the_figures_dir(self):
        self.assertIsNone(figures._safe_figure_path("../outside.png"))
        self.assertIsNone(figures._safe_figure_path(str(self.dir / "abs.png")))
        path = self._write_png("pmid_1/fig_a.png")
        self.assertEqual(
            figures._safe_figure_path("pmid_1/fig_a.png"), path.resolve()
        )

    def test_per_paper_replacement_preserves_other_papers(self):
        self._write_png("pmid_1/fig_old.png")
        self._write_png("pmid_2/fig_other.png")
        figures.save_index([
            self._entry("aaaaaaaa", "pmid:1", "pmid_1/fig_old.png"),
            self._entry("bbbbbbbb", "pmid:2", "pmid_2/fig_other.png",
                        label="Figure 9"),
        ])
        pdf = self.paper_pdf(name="new.pdf", caption="Figure 3. Replaced caption.")
        figures.process_paper(pdf, "pmid:1", title="New")
        loaded = figures.load_index()
        self.assertEqual({entry["paper_key"] for entry in loaded}, {"pmid:1", "pmid:2"})
        pmid_1 = [entry for entry in loaded if entry["paper_key"] == "pmid:1"]
        self.assertEqual(len(pmid_1), 1)
        self.assertEqual(pmid_1[0]["label"], "Figure 3")
        self.assertNotEqual(pmid_1[0]["id"], "aaaaaaaa")
        self.assertEqual(
            [entry["id"] for entry in loaded if entry["paper_key"] == "pmid:2"],
            ["bbbbbbbb"],
        )

    def test_api_list_filters_missing_files_and_paper_key(self):
        self._write_png("pmid_1/fig_a.png")
        figures.save_index([
            self._entry("aaaaaaaa", "pmid:1", "pmid_1/fig_a.png"),
            self._entry("bbbbbbbb", "pmid:1", "pmid_1/fig_missing.png",
                        label="Figure 2"),
            self._entry("cccccccc", "pmid:2", "pmid_2/fig_other.png"),
        ])
        all_entries = figures.api_list()
        self.assertEqual([entry["id"] for entry in all_entries], ["aaaaaaaa"])
        self.assertEqual(figures.api_list("pmid:2"), [])
        self.assertEqual(figures.api_list("unknown"), [])

    def test_resolve_figure_path(self):
        self._write_png("pmid_1/fig_a.png")
        figures.save_index([self._entry("aaaaaaaa", "pmid:1", "pmid_1/fig_a.png")])
        path = figures.resolve_figure_path("aaaaaaaa")
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())
        self.assertIsNone(figures.resolve_figure_path("unknown"))
        self.assertIsNone(figures.resolve_figure_path(""))
        self.assertIsNone(figures.resolve_figure_path("../../etc/passwd"))

    def test_figures_xlsx_has_the_documented_header_and_rows(self):
        self._write_png("pmid_1/fig_a.png")
        figures.save_index([self._entry("aaaaaaaa", "pmid:1", "pmid_1/fig_a.png")])
        figures.write_figures_xlsx()
        table = read_sheet(self.xlsx_path)
        self.assertEqual(table[0], figures.FIGURE_COLUMNS)
        self.assertEqual(len(table), 2)
        row = dict(zip(table[0], table[1]))
        self.assertEqual(row["id"], "aaaaaaaa")
        self.assertEqual(row["paper_key"], "pmid:1")
        self.assertEqual(row["file"], "pmid_1/fig_a.png")


class FiguresApiTests(FigureTestBase):
    def setUp(self):
        super().setUp()
        self.client = web.app.test_client()
        self.pdf = self.paper_pdf(caption="Figure 1. API caption.")
        figures.process_paper(self.pdf, "pmid:1", title="Paper One")
        figures.process_paper(
            make_pdf(self.dir / "other.pdf", [caption_page("Figure 2. Other.")]),
            "pmid:2",
            title="Paper Two",
        )
        self.entry = figures.load_index()[0]

    def _result_patches(self):
        return [
            mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", self.dir / "search.xlsx"),
            mock.patch.object(web.config, "EXTRACTED_XLSX", self.dir / "extracted.xlsx"),
            mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", self.dir / "closed.xlsx"),
        ]

    def test_figure_png_is_served_by_index_id(self):
        response = self.client.get(f"/api/figure/{self.entry['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertTrue(response.data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_unknown_and_traversal_ids_are_404(self):
        self.assertEqual(self.client.get("/api/figure/unknown").status_code, 404)
        self.assertEqual(
            self.client.get("/api/figure/..%2F..%2Fetc%2Fpasswd").status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/api/figure/%2e%2e%2fsecret").status_code, 404
        )

    def test_malicious_index_entry_is_not_served(self):
        self.index_path.write_text(json.dumps({
            "version": 1,
            "figures": [{
                **self.entry,
                "id": "deadbeef",
                "file": "../../secret.png",
            }],
        }), encoding="utf-8")
        self.assertEqual(self.client.get("/api/figure/deadbeef").status_code, 404)

    def test_figures_endpoint_filters_by_paper_key(self):
        all_payload = self.client.get("/api/figures").get_json()
        self.assertEqual(len(all_payload["figures"]), 2)
        one = self.client.get("/api/figures?paper_key=pmid:1").get_json()
        self.assertEqual(
            [entry["paper_key"] for entry in one["figures"]], ["pmid:1"]
        )
        none = self.client.get("/api/figures?paper_key=missing").get_json()
        self.assertEqual(none["figures"], [])

    def test_results_include_figures_and_relative_paths_only(self):
        with contextlib.ExitStack() as stack:
            for patcher in self._result_patches():
                stack.enter_context(patcher)
            payload = self.client.get("/api/results").get_json()
        self.assertEqual(
            set(payload),
            {"search", "extracted", "closed", "annotations", "figures"},
        )
        self.assertEqual(len(payload["figures"]), 2)
        self.assertNotIn(str(self.dir), json.dumps(payload["figures"]))

    def test_download_allowlist_serves_figures_xlsx(self):
        response = self.client.get("/download/figures.xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("Content-Disposition", "").split(";")[0],
            "attachment",
        )
        self.assertEqual(
            self.client.get("/download/not_a_file.xlsx").status_code, 404
        )


class IntegrationTests(FigureTestBase):
    def test_main_run_extract_calls_figure_processing_even_with_zero_records(self):
        pdf = self.paper_pdf()
        processed = []
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(main.config, "EXTRACTED_XLSX", tmp / "extracted.xlsx"), \
                 mock.patch.object(main.config, "CLOSED_SOURCE_XLSX", tmp / "closed.xlsx"), \
                 mock.patch.object(main.config, "CHECKPOINT_JSON", tmp / "checkpoint.json"), \
                 mock.patch.object(main.config, "EVIDENCE_ENABLED", False), \
                 mock.patch.object(main.config, "PDF_ARCHIVE_ENABLED", False), \
                 mock.patch.object(main.extractor, "check_backend", lambda: None), \
                 mock.patch.object(
                     main,
                     "_prepare_paper",
                     lambda p: ("full text " * 100, "test", {
                         "path": pdf,
                         "filename": pdf.name,
                         "label": "article:pmc",
                         "reason": "",
                     }),
                 ), \
                 mock.patch.object(main.extractor, "extract", lambda *a, **k: []), \
                 mock.patch.object(
                     main.figures,
                     "process_paper",
                     side_effect=lambda *a, **k: processed.append((a, k)) or {
                         "saved": 0, "duplicates": 0, "capped": False, "warning": "",
                     },
                 ):
                main.run_extract(
                    papers=[{
                        "title": "Paper", "doi": "10.1/one", "pmid": "111",
                        "sources": ["pubmed"],
                    }],
                    mode="records",
                )
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0][0][0], pdf)
        self.assertEqual(processed[0][0][1], "pmid:111")
        self.assertEqual(processed[0][1]["goal"], main.config.INSTRUCTION)

    def test_main_figure_failure_does_not_lose_extraction(self):
        pdf = self.paper_pdf()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            extracted = tmp / "extracted.xlsx"
            with mock.patch.object(main.config, "EXTRACTED_XLSX", extracted), \
                 mock.patch.object(main.config, "CLOSED_SOURCE_XLSX", tmp / "closed.xlsx"), \
                 mock.patch.object(main.config, "CHECKPOINT_JSON", tmp / "checkpoint.json"), \
                 mock.patch.object(main.config, "EVIDENCE_ENABLED", False), \
                 mock.patch.object(main.config, "PDF_ARCHIVE_ENABLED", False), \
                 mock.patch.object(main.extractor, "check_backend", lambda: None), \
                 mock.patch.object(
                     main,
                     "_prepare_paper",
                     lambda p: ("full text " * 100, "test", {
                         "path": pdf, "filename": pdf.name,
                         "label": "article:pmc", "reason": "",
                     }),
                 ), \
                 mock.patch.object(
                     main.extractor, "extract",
                     lambda *a, **k: [{"subunit": "a4"}],
                 ), \
                 mock.patch.object(
                     main.figures, "process_paper",
                     side_effect=RuntimeError("figure engine exploded"),
                 ), contextlib.redirect_stdout(output):
                main.run_extract(
                    papers=[{
                        "title": "Paper", "doi": "10.1/one", "pmid": "111",
                        "sources": ["pubmed"],
                    }],
                    mode="records",
                )
            table = read_sheet(extracted)
        self.assertEqual(len(table), 2)
        self.assertIn("figure warning: figure engine exploded", output.getvalue())

    def _run_web_extract(self, process_mock):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pdf = make_pdf(tmp / "paper.pdf", [caption_page()])
            job = {
                "log": [],
                "progress": {"done": 0, "total": 0, "current": ""},
                "result": None,
                "status": "running",
                "error": None,
            }
            paper = {
                "title": "Paper One", "doi": "10.1/new", "pmid": "555",
                "sources": ["pubmed"],
            }
            with mock.patch.object(web.history, "record"), \
                 mock.patch.object(web.config, "EXTRACTED_XLSX", tmp / "extracted.xlsx"), \
                 mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", tmp / "closed.xlsx"), \
                 mock.patch.object(web.config, "CHECKPOINT_JSON", tmp / "checkpoint.json"), \
                 mock.patch.object(web.config, "PDF_ARCHIVE_ENABLED", False), \
                 mock.patch.object(web.config, "FIGURES_ENABLED", True), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(web, "load_settings", lambda: {}), \
                 mock.patch.object(web, "_load_search_results", lambda required=False: [paper]), \
                 mock.patch.object(
                     web,
                     "_prepare_paper",
                     lambda p: ("full text " * 100, "test", {
                         "path": pdf, "filename": pdf.name,
                         "label": "article:pmc", "reason": "",
                     }),
                 ), \
                 mock.patch.object(
                     web.extractor, "extract",
                     lambda *a, **k: [{"mutation": "M2"}],
                 ), \
                 mock.patch.object(
                     web.pdf_annotations, "annotate_paper_rows",
                     lambda rows, path, **kwargs: (rows, {
                         "located": 0, "unresolved": 0, "warning": "",
                         "evidence": [],
                     }),
                 ), \
                 mock.patch.object(web.figures, "process_paper", process_mock):
                web._run_extract_job(job, "Extract mutations", ["mutation"])
            return job

    def test_web_extract_reports_figure_counts(self):
        process_mock = mock.Mock(return_value={
            "saved": 2,
            "candidates": 5,
            "matched": 2,
            "skipped": 3,
            "duplicates": 1,
            "capped": True,
            "candidate_capped": True,
            "goal_filtered": True,
            "warning": "",
        })
        job = self._run_web_extract(process_mock)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["figures_saved"], 2)
        self.assertEqual(job["result"]["figures_candidates"], 5)
        self.assertEqual(job["result"]["figures_matched"], 2)
        self.assertEqual(job["result"]["figures_skipped"], 3)
        self.assertEqual(job["result"]["figure_duplicates"], 1)
        self.assertEqual(job["result"]["figures_capped"], 1)
        self.assertEqual(job["result"]["figure_candidate_capped"], 1)
        self.assertEqual(
            process_mock.call_args.kwargs["goal"], "Extract mutations"
        )
        self.assertIn(
            "2 figure screenshot(s) matched the extraction goal "
            "(from 5 candidate(s))",
            "\n".join(job["log"]),
        )

    def test_web_extract_figure_failure_only_warns(self):
        job = self._run_web_extract(
            mock.Mock(side_effect=RuntimeError("broken figure engine"))
        )
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["figure_warnings"], 1)
        self.assertIn("figure warning: broken figure engine", "\n".join(job["log"]))

    def test_web_search_clears_old_figures(self):
        self.figures_dir.mkdir(parents=True, exist_ok=True)
        png = self.figures_dir / "pmid_1" / "fig_old.png"
        png.parent.mkdir(parents=True)
        png.write_bytes(b"\x89PNG\r\n\x1a\nold")
        figures.save_index([{
            "id": "aaaaaaaa", "paper_key": "pmid:1", "title": "Old",
            "label": "Figure 1", "caption": "", "page": 1,
            "file": "pmid_1/fig_old.png", "kind": "caption",
            "width": 1, "height": 1, "dpi": 150,
            "created_at": "2026-01-01T00:00:00Z",
        }])
        self.xlsx_path.write_bytes(b"old xlsx")

        job = {
            "log": [],
            "progress": {"done": 0, "total": 0, "current": ""},
            "result": None, "status": "running", "error": None,
        }
        with mock.patch.object(web.history, "record"), \
             mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", self.dir / "search.xlsx"), \
             mock.patch.object(web.config, "EXTRACTED_XLSX", self.dir / "extracted.xlsx"), \
             mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", self.dir / "closed.xlsx"), \
             mock.patch.object(web.config, "CHECKPOINT_JSON", self.dir / "checkpoint.json"), \
             mock.patch.object(web.config, "LOCAL_PDF_INDEX_JSON", self.dir / "local.json"), \
             mock.patch.object(web, "apply_settings", lambda s: None), \
             mock.patch.object(web, "load_settings", lambda: {}), \
             mock.patch.object(
                 web.searchers, "search",
                 lambda *a, **k: [{"sources": ["pubmed"], "title": "New paper",
                                   "doi": "10.1/new"}],
             ):
            web._run_search_job(job, "A", ["pubmed"], 10)
        self.assertEqual(job["status"], "done")
        self.assertEqual(figures.load_index(), [])
        self.assertFalse(self.xlsx_path.exists())
        self.assertFalse(png.exists())

    def test_cli_search_preserves_existing_figures(self):
        self.figures_dir.mkdir(parents=True, exist_ok=True)
        figures.save_index([{
            "id": "aaaaaaaa", "paper_key": "pmid:1", "title": "Old",
            "label": "Figure 1", "caption": "", "page": 1,
            "file": "pmid_1/fig_old.png", "kind": "caption",
            "width": 1, "height": 1, "dpi": 150,
            "created_at": "2026-01-01T00:00:00Z",
        }])
        with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", self.dir / "search.xlsx"), \
             mock.patch.object(main.config, "EXCLUDED_JOURNALS", []), \
             mock.patch.object(main.searchers, "search_all", lambda **kwargs: []), \
             contextlib.redirect_stdout(io.StringIO()):
            main.run_search(year_from="", year_to="")
        self.assertEqual([entry["id"] for entry in figures.load_index()], ["aaaaaaaa"])


if __name__ == "__main__":
    unittest.main()
