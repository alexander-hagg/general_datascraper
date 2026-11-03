"""Writer tests: header migration and header-driven appends."""

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

import writer


def make_workbook(path, header, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    wb.save(path)


def read_sheet(path):
    wb = load_workbook(path, read_only=True)
    ws = wb.active
    rows = [["" if c is None else c for c in row] for row in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


class WriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_ensure_columns_preserves_old_values_and_adds_defaults(self):
        path = self.dir / "old.xlsx"
        make_workbook(path, ["doi", "title"], [["10.1/a", "Old paper"], ["10.1/b", "Second"]])

        result = writer.ensure_columns(
            path,
            ["paper_key", "doi", "row_index", "evidence"],
            defaults={"evidence": "N/A"},
        )

        self.assertTrue(result)
        rows = read_sheet(path)
        self.assertEqual(
            rows[0],
            ["doi", "title", "paper_key", "row_index", "evidence"],
        )
        self.assertEqual(rows[1][:2], ["10.1/a", "Old paper"])
        self.assertEqual(rows[2][:2], ["10.1/b", "Second"])
        self.assertEqual(rows[1][4], "N/A")
        self.assertEqual(rows[2][4], "N/A")
        self.assertEqual(rows[1][2], "")
        self.assertEqual(rows[1][3], "")

    def test_ensure_columns_missing_file_does_nothing(self):
        path = self.dir / "does-not-exist.xlsx"
        self.assertFalse(writer.ensure_columns(path, ["paper_key"]))
        self.assertFalse(path.exists())

    def test_ensure_columns_keeps_existing_order_and_avoids_duplicates(self):
        path = self.dir / "old.xlsx"
        make_workbook(path, ["b", "a"], [["B", "A"]])
        writer.ensure_columns(path, ["a", "b", "c"])
        rows = read_sheet(path)
        self.assertEqual(rows[0], ["b", "a", "c"])
        self.assertEqual(rows[1], ["B", "A", ""])

    def test_append_maps_values_after_header_migration(self):
        path = self.dir / "extracted.xlsx"
        make_workbook(
            path,
            ["doi", "source", "subunit"],
            [["10.1/a", "pubmed", "alpha4"]],
        )

        writer.append_xlsx(
            path,
            ["paper_key", "row_index", "doi", "source", "pdf_file", "subunit"],
            [{
                "paper_key": "doi:10.1/b",
                "row_index": 1,
                "doi": "10.1/b",
                "source": "crossref",
                "pdf_file": "PMID_2.pdf",
                "subunit": "beta2",
            }],
        )

        rows = read_sheet(path)
        header = rows[0]
        self.assertEqual(
            header,
            ["doi", "source", "subunit", "paper_key", "row_index", "pdf_file"],
        )
        old = dict(zip(header, rows[1]))
        new = dict(zip(header, rows[2]))
        self.assertEqual(old["doi"], "10.1/a")
        self.assertEqual(old["subunit"], "alpha4")
        self.assertTrue(all(old[c] == "" for c in ("paper_key", "row_index", "pdf_file")))
        self.assertEqual(new["paper_key"], "doi:10.1/b")
        self.assertEqual(new["row_index"], 1)
        self.assertEqual(new["doi"], "10.1/b")
        self.assertEqual(new["source"], "crossref")
        self.assertEqual(new["pdf_file"], "PMID_2.pdf")
        self.assertEqual(new["subunit"], "beta2")

    def test_append_respects_reordered_sheet_header(self):
        path = self.dir / "reordered.xlsx"
        make_workbook(path, ["b", "a"], [["old-b", "old-a"]])

        writer.append_xlsx(path, ["a", "b"], [{"a": "new-a", "b": "new-b"}])

        rows = read_sheet(path)
        self.assertEqual(rows[0], ["b", "a"])
        self.assertEqual(rows[1], ["old-b", "old-a"])
        self.assertEqual(rows[2], ["new-b", "new-a"])

    def test_append_does_not_shift_values_around_blank_header_cells(self):
        path = self.dir / "gap.xlsx"
        make_workbook(path, ["a", None, "b"], [["old-a", None, "old-b"]])

        writer.append_xlsx(path, ["a", "b"], [{"a": "new-a", "b": "new-b"}])

        rows = read_sheet(path)
        self.assertEqual(rows[1], ["old-a", "", "old-b"])
        self.assertEqual(rows[2], ["new-a", "", "new-b"])

    def test_append_creates_file_with_requested_header(self):
        path = self.dir / "new.xlsx"
        writer.append_xlsx(path, ["paper_key", "row_index"], [{"paper_key": "pmid:1", "row_index": 2}])
        self.assertEqual(read_sheet(path), [["paper_key", "row_index"], ["pmid:1", 2]])

    def test_backfill_paper_keys_uses_only_doi(self):
        path = self.dir / "extracted.xlsx"
        make_workbook(
            path,
            ["doi", "paper_key", "subunit"],
            [
                ["10.1/ABC", "", "one"],
                ["", "", "no-doi"],
                ["10.1/def", "pmid:9", "already-keyed"],
            ],
        )

        filled = writer.backfill_paper_keys(path)

        self.assertEqual(filled, 1)
        rows = read_sheet(path)
        self.assertEqual(rows[1][1], "doi:10.1/abc")
        self.assertEqual(rows[2][1], "")  # never invent an identity
        self.assertEqual(rows[3][1], "pmid:9")  # existing key untouched


if __name__ == "__main__":
    unittest.main()
