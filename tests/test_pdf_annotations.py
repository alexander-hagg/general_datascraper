"""Tests for evidence quote location, PDF annotation cache and the index."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

import config
import pdf_annotations


def make_pdf(path, pages):
    """Create a real PDF; `pages` is a list of lists of text lines."""
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


def annotation_count(path):
    doc = pymupdf.open(str(path))
    try:
        return sum(len(list(page.annots())) for page in doc)
    finally:
        doc.close()


def annotation_contents(path):
    doc = pymupdf.open(str(path))
    try:
        return [
            annot.info.get("content", "")
            for page in doc
            for annot in page.annots()
        ]
    finally:
        doc.close()


def annotation_details(path):
    """Return type/colour/content for every annotation in a PDF."""
    doc = pymupdf.open(str(path))
    try:
        return [
            {
                "type": annot.type,
                "colors": dict(annot.colors),
                "content": annot.info.get("content", ""),
                "title": annot.info.get("title", ""),
            }
            for page in doc
            for annot in page.annots()
        ]
    finally:
        doc.close()


class PdfAnnotationTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.pdf_dir = self.dir / "pdfs"
        self.pdf_dir.mkdir()
        self.annotated_dir = self.dir / "annotated_pdfs"
        self.index_path = self.dir / "pdf_index.json"
        patches = [
            mock.patch.object(config, "PDF_DIR", self.pdf_dir),
            mock.patch.object(config, "ANNOTATED_PDF_DIR", self.annotated_dir),
            mock.patch.object(config, "PDF_INDEX_JSON", self.index_path),
            mock.patch.object(config, "EVIDENCE_ENABLED", True),
            mock.patch.object(config, "EVIDENCE_FUZZY_THRESHOLD", 0.75),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.source = make_pdf(
            self.pdf_dir / "paper.pdf",
            [
                ["Introduction only."],
                ["The mutation increased current in oocytes."],
            ],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def annotate(self, quotes, key="pmid:1", source=None):
        rows = [{"row_index": i, "evidence": q} for i, q in enumerate(quotes, 1)]
        return self.annotate_rows(rows, key=key, source=source)

    def annotate_rows(self, rows, key="pmid:1", source=None):
        return pdf_annotations.annotate_paper_rows(
            rows,
            source or self.source,
            paper_key=key,
            title="A paper",
            pdf_file=(source or self.source).name,
            pdf_source="article:pmc",
        )


class NormalizeAndLocateTests(PdfAnnotationTestBase):
    def test_normalize_case_whitespace_and_hyphenation(self):
        self.assertEqual(
            pdf_annotations.normalize_text("The  Muta-\ntion\nwas\tfound"),
            "the mutation was found",
        )
        self.assertEqual(
            pdf_annotations.normalize_text("soft\u00adhyphen and ﬁgure"),
            "softhyphen and figure",
        )

    def test_exact_match_uses_one_based_page_numbers(self):
        doc = pymupdf.open(str(self.source))
        try:
            page_number, rects, status = pdf_annotations.locate_quote(
                doc[1], "mutation increased current"
            )
        finally:
            doc.close()
        self.assertEqual(page_number, 2)
        self.assertEqual(status, "exact")
        self.assertTrue(rects)

    def test_fuzzy_match_and_unresolved(self):
        doc = pymupdf.open(str(self.source))
        try:
            page_number, rects, status = pdf_annotations.locate_quote(
                doc[1], "mutation increased curent in oocytes"
            )
            self.assertEqual(page_number, 2)
            self.assertEqual(status, "fuzzy")
            self.assertTrue(rects)

            page_number, rects, status = pdf_annotations.locate_quote(
                doc[0], "a sentence that is absent"
            )
            self.assertEqual(page_number, 1)
            self.assertEqual(status, "unresolved")
            self.assertEqual(rects, [])
        finally:
            doc.close()

    def test_block_list_input_is_supported(self):
        doc = pymupdf.open(str(self.source))
        try:
            blocks = doc[1].get_text("blocks")
        finally:
            doc.close()
        page_number, rects, status = pdf_annotations.locate_quote(
            blocks, "mutation increased current"
        )
        self.assertEqual(page_number, 1)
        self.assertEqual(status, "exact")
        self.assertTrue(rects)


class AnnotationCreationTests(PdfAnnotationTestBase):
    def test_rows_pages_annotation_and_index_statuses(self):
        rows, summary = self.annotate([
            "mutation increased current",
            "mutation increased curent in oocytes",
            "not present anywhere",
        ])

        self.assertEqual([r["evidence_page"] for r in rows], ["2", "2", ""])
        self.assertEqual(summary["located"], 2)
        self.assertEqual(summary["unresolved"], 1)
        self.assertTrue(summary["available"])
        self.assertTrue(summary["annotation_file"])

        annotated = self.annotated_dir / summary["annotation_file"]
        self.assertTrue(annotated.exists())
        self.assertGreaterEqual(annotation_count(annotated), 2)
        contents = "\n".join(annotation_contents(annotated))
        self.assertIn("mutation increased current", contents)
        # The source PDF stays untouched.
        self.assertEqual(annotation_count(self.source), 0)

        index = pdf_annotations.load_index()
        entry = index["pmid:1"]
        self.assertEqual(entry["pdf_file"], "paper.pdf")
        self.assertEqual(entry["annotation_file"], annotated.name)
        self.assertEqual(
            [item["status"] for item in entry["evidence"]],
            ["exact", "fuzzy", "unresolved"],
        )
        self.assertEqual(
            [item["page"] for item in entry["evidence"]], [2, 2, None]
        )

    def test_highlights_are_yellow_and_include_row_data(self):
        rows = [
            {
                "row_index": 1,
                "paper_key": "META_PAPER_KEY_SENTINEL",
                "doi": "META_DOI_SENTINEL",
                "source": "META_SOURCE_SENTINEL",
                "publication_type": "META_TYPE_SENTINEL",
                "pdf_file": "META_PDF_SENTINEL",
                "pdf_source": "META_PDF_SOURCE_SENTINEL",
                "evidence": "mutation increased current",
                "evidence_page": "99",
                "mutation": "A123T",
                "effect": "loss of function",
            },
            {
                "row_index": 2,
                "mutation": "V456L",
                "evidence": "mutation increased curent in oocytes",
            },
        ]
        _rows, summary = self.annotate_rows(rows)
        self.assertTrue(summary["annotation_file"])
        annotated = self.annotated_dir / summary["annotation_file"]

        details = annotation_details(annotated)
        self.assertGreaterEqual(len(details), 2)
        for annot in details:
            self.assertEqual(annot["type"][1], "Highlight")
            self.assertEqual(tuple(annot["colors"]["stroke"]), (1.0, 1.0, 0.0))
            self.assertEqual(annot["title"], "ScrapeLite evidence")

        contents = "\n".join(item["content"] for item in details)
        self.assertIn(
            'Data: mutation=A123T; effect=loss of function\n'
            'Evidence: "mutation increased current"',
            contents,
        )
        self.assertIn(
            'Data: mutation=V456L\n'
            'Evidence: "mutation increased curent in oocytes"',
            contents,
        )
        for sentinel in (
            "META_PAPER_KEY_SENTINEL",
            "META_DOI_SENTINEL",
            "META_SOURCE_SENTINEL",
            "META_TYPE_SENTINEL",
            "META_PDF_SENTINEL",
            "META_PDF_SOURCE_SENTINEL",
        ):
            self.assertNotIn(sentinel, contents)

    def test_rows_without_data_fall_back_to_a_row_label(self):
        _rows, summary = self.annotate(["mutation increased current"])
        annotated = self.annotated_dir / summary["annotation_file"]
        contents = "\n".join(annotation_contents(annotated))
        self.assertIn('Row 1\nEvidence: "mutation increased current"', contents)
        self.assertNotIn("Data:", contents)

    def test_changed_row_data_changes_hash_and_popup(self):
        quote = "mutation increased current"
        _rows, first = self.annotate_rows([
            {"row_index": 1, "mutation": "A123T", "evidence": quote},
        ])
        _rows, second = self.annotate_rows([
            {"row_index": 1, "mutation": "V456L", "evidence": quote},
        ])
        self.assertNotEqual(first["annotation_file"], second["annotation_file"])

        first_contents = "\n".join(
            annotation_contents(self.annotated_dir / first["annotation_file"])
        )
        second_contents = "\n".join(
            annotation_contents(self.annotated_dir / second["annotation_file"])
        )
        self.assertIn("mutation=A123T", first_contents)
        self.assertNotIn("mutation=V456L", first_contents)
        self.assertIn("mutation=V456L", second_contents)
        self.assertNotIn("mutation=A123T", second_contents)

    def test_regeneration_keeps_the_row_data(self):
        _rows, summary = self.annotate_rows([
            {
                "row_index": 1,
                "mutation": "A123T",
                "effect": "loss of function",
                "evidence": "mutation increased current",
            },
        ])
        entry = pdf_annotations.load_index()["pmid:1"]
        self.assertEqual(
            entry["evidence"][0]["data"],
            {"mutation": "A123T", "effect": "loss of function"},
        )

        path = self.annotated_dir / summary["annotation_file"]
        path.unlink()
        regenerated = pdf_annotations.regenerate_annotation("pmid:1")
        self.assertIsNotNone(regenerated)
        contents = "\n".join(annotation_contents(regenerated))
        self.assertIn("Data: mutation=A123T; effect=loss of function", contents)
        self.assertIn('Evidence: "mutation increased current"', contents)
        statuses = [
            item["status"]
            for item in pdf_annotations.load_index()["pmid:1"]["evidence"]
        ]
        self.assertEqual(statuses, ["exact"])

    def test_non_string_values_are_rendered_and_bounded(self):
        _rows, summary = self.annotate_rows([
            {
                "row_index": 1,
                "evidence": "mutation increased current",
                "count": 3,
                "tags": ["alpha", "beta"],
                "details": {"note": "kept", "blank": ""},
                "missing": None,
                "empty": "",
                "long": "x" * 1000,
            },
        ])
        contents = "\n".join(
            annotation_contents(self.annotated_dir / summary["annotation_file"])
        )
        self.assertIn("count=3", contents)
        self.assertIn("tags=alpha, beta", contents)
        self.assertIn("details=note=kept", contents)
        self.assertNotIn("missing=", contents)
        self.assertNotIn("empty=", contents)
        self.assertLessEqual(
            len(pdf_annotations._stringify_data_value("x" * 1000)),
            pdf_annotations.ANNOTATION_VALUE_MAX_CHARS,
        )
        self.assertLessEqual(
            len(pdf_annotations._format_data({"long": "x" * 1000})),
            pdf_annotations.ANNOTATION_DATA_MAX_CHARS,
        )

    def test_cache_reuse_and_regeneration(self):
        _rows, first = self.annotate(["mutation increased current"])
        path = self.annotated_dir / first["annotation_file"]
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        count = annotation_count(path)

        _rows, second = self.annotate(["mutation increased current"])
        self.assertEqual(second["annotation_file"], first["annotation_file"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(annotation_count(path), count)

        path.unlink()
        regenerated = pdf_annotations.regenerate_annotation("pmid:1")
        self.assertIsNotNone(regenerated)
        self.assertTrue(regenerated.exists())
        self.assertGreaterEqual(annotation_count(regenerated), 1)

    def test_changed_evidence_or_source_changes_annotation_filename(self):
        _rows, first = self.annotate(["mutation increased current"])
        _rows, second = self.annotate([
            "mutation increased current",
            "introduction only",
        ])
        self.assertNotEqual(first["annotation_file"], second["annotation_file"])

        changed_source = make_pdf(
            self.pdf_dir / "changed.pdf",
            [["The mutation increased current in oocytes.", "Extra line."]],
        )
        _rows, third = self.annotate(
            ["mutation increased current"], key="pmid:2", source=changed_source
        )
        self.assertNotEqual(first["annotation_file"], third["annotation_file"])

    def test_no_resolved_evidence_still_registers_the_pdf(self):
        _rows, summary = self.annotate(["a sentence that is absent"])
        self.assertEqual(summary["located"], 0)
        self.assertEqual(summary["unresolved"], 1)
        self.assertEqual(summary["annotation_file"], "")
        index = pdf_annotations.load_index()
        self.assertIn("pmid:1", index)
        self.assertEqual(index["pmid:1"]["annotation_file"], "")
        api = pdf_annotations.api_summary()
        self.assertTrue(api["pmid:1"]["available"])
        resolved = pdf_annotations.resolve_paper_paths("pmid:1")
        self.assertEqual(resolved["source_path"], self.source.resolve())
        self.assertIsNone(resolved["annotation_path"])

    def test_index_stores_only_basenames(self):
        self.annotate(["mutation increased current"])
        text = self.index_path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.dir), text)
        entry = json.loads(text)["pmid:1"]
        self.assertEqual(Path(entry["pdf_file"]).name, entry["pdf_file"])
        self.assertEqual(
            Path(entry["annotation_file"]).name, entry["annotation_file"]
        )


class IndexSafetyTests(PdfAnnotationTestBase):
    def _entry(self, **overrides):
        entry = {
            "paper_key": "pmid:1",
            "pdf_file": "paper.pdf",
            "pdf_source": "article:pmc",
            "title": "A paper",
            "annotation_file": "",
            "annotation_hash": "",
            "evidence": [],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        entry.update(overrides)
        return entry

    def test_index_round_trip_and_atomic_write(self):
        pdf_annotations.save_index({"pmid:1": self._entry()})
        self.assertTrue(self.index_path.exists())
        loaded = pdf_annotations.load_index()
        self.assertEqual(loaded["pmid:1"]["pdf_file"], "paper.pdf")
        self.assertFalse(
            [p for p in self.dir.iterdir() if p.name.endswith(".tmp")]
        )

        self.index_path.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(pdf_annotations.load_index(), {})
        self.assertEqual(pdf_annotations.api_summary(), {})

    def test_traversal_and_absolute_filenames_are_rejected(self):
        for bad in ("../secret.pdf", "/etc/passwd", "..\\secret.pdf"):
            self.index_path.write_text(
                json.dumps({"pmid:1": self._entry(pdf_file=bad)}),
                encoding="utf-8",
            )
            self.assertEqual(pdf_annotations.load_index(), {})
            self.assertIsNone(pdf_annotations.resolve_paper_paths("pmid:1"))

        self.assertIsNone(
            pdf_annotations.resolve_paper_paths(
                "pmid:1",
                index={"pmid:1": self._entry(annotation_file="../../x.pdf")},
            )
        )

    def test_evidence_data_is_sanitized(self):
        entry = self._entry(evidence=[{
            "row_index": 1,
            "text": "mutation increased current",
            "page": 2,
            "status": "exact",
            "data": {
                "mutation": "A123T",
                "paper_key": "SENTINEL",
                "doi": "SENTINEL",
                "empty": "",
            },
        }])
        pdf_annotations.save_index({"pmid:1": entry})
        evidence = pdf_annotations.load_index()["pmid:1"]["evidence"][0]
        self.assertEqual(evidence["data"], {"mutation": "A123T"})

        summary = pdf_annotations.api_summary()["pmid:1"]
        self.assertEqual(
            set(summary["evidence"][0]),
            {"row_index", "text", "page", "status"},
        )

    def test_unknown_key_returns_none(self):
        pdf_annotations.save_index({"pmid:1": self._entry()})
        self.assertIsNone(pdf_annotations.resolve_paper_paths("pmid:unknown"))
        self.assertIsNone(pdf_annotations.resolve_paper_paths(""))


class BestEffortTests(PdfAnnotationTestBase):
    def test_missing_pdf_keeps_evidence_and_blanks_the_page(self):
        rows = [{"row_index": 1, "evidence": "some quote", "evidence_page": "9"}]
        returned, summary = pdf_annotations.annotate_paper_rows(
            rows, None, paper_key="pmid:1", title="A paper"
        )
        self.assertEqual(returned[0]["evidence"], "some quote")
        self.assertEqual(returned[0]["evidence_page"], "")
        self.assertIn("No archived PDF", summary["warning"])
        self.assertEqual(pdf_annotations.load_index(), {})

    def test_disabled_evidence_returns_rows_unchanged(self):
        rows = [{"row_index": 1, "evidence": "mutation increased current"}]
        with mock.patch.object(config, "EVIDENCE_ENABLED", False):
            returned, summary = pdf_annotations.annotate_paper_rows(
                rows, self.source, paper_key="pmid:1", pdf_file="paper.pdf"
            )
        self.assertEqual(returned, rows)
        self.assertNotIn("evidence_page", returned[0])
        self.assertIn("disabled", summary["warning"])
        self.assertEqual(pdf_annotations.load_index(), {})

    def test_missing_pymupdf_is_a_warning_not_an_error(self):
        rows = [{"row_index": 1, "evidence": "mutation increased current"}]
        with mock.patch.object(pdf_annotations, "PYMUPDF_AVAILABLE", False):
            returned, summary = pdf_annotations.annotate_paper_rows(
                rows, self.source, paper_key="pmid:1", pdf_file="paper.pdf"
            )
        self.assertEqual(returned, rows)
        self.assertIn("PyMuPDF", summary["warning"])
        self.assertEqual(rows[0]["evidence"], "mutation increased current")


if __name__ == "__main__":
    unittest.main()
