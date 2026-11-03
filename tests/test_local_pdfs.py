"""Tests for the local PDF folder workflow."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import local_pdfs
import main
import searchers


def _add_text_page(writer, text):
    """Add one page with a Helvetica text line pypdf can extract."""
    page = writer.add_blank_page(width=612, height=792)
    if not text:
        return page
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
    })
    stream = DecodedStreamObject()
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    return page


def make_pdf(path, title=None, author=None, creation=None, doi=None,
             page_texts=None, encrypt=None):
    """Write a tiny valid PDF with optional metadata/text (no network)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for text in (page_texts or [""]):
        _add_text_page(writer, text)
    metadata = {}
    if title is not None:
        metadata["/Title"] = title
    if author is not None:
        metadata["/Author"] = author
    if creation is not None:
        metadata["/CreationDate"] = creation
    if doi is not None:
        metadata["/doi"] = doi
    if metadata:
        writer.add_metadata(metadata)
    if encrypt:
        writer.encrypt(encrypt)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


class LocalPdfTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.archive = self.root / "archive"
        self.index = self.root / "local_pdfs.json"
        self._patchers = [
            mock.patch.object(local_pdfs.config, "PDF_DIR", self.archive),
            mock.patch.object(local_pdfs.config, "LOCAL_PDF_INDEX_JSON", self.index),
            mock.patch.object(local_pdfs.config, "MAX_LOCAL_PDFS", 500),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()


class NormalizationTests(unittest.TestCase):
    def test_doi_forms_normalize_to_the_same_value(self):
        expected = "10.1000/abc.1"
        self.assertEqual(local_pdfs.normalize_doi(" 10.1000/ABC.1 "), expected)
        self.assertEqual(local_pdfs.normalize_doi("doi: 10.1000/ABC.1"), expected)
        self.assertEqual(
            local_pdfs.normalize_doi("https://doi.org/10.1000/ABC.1"), expected
        )
        self.assertEqual(
            local_pdfs.normalize_doi("(10.1000/ABC.1)."), expected
        )

    def test_non_doi_values_are_empty(self):
        self.assertEqual(local_pdfs.normalize_doi("not a doi"), "")
        self.assertEqual(local_pdfs.normalize_doi(None), "")

    def test_titles_ignore_case_punctuation_and_whitespace(self):
        self.assertEqual(
            local_pdfs.normalize_title("  The  Study: Of—Things! "),
            "the study of things",
        )
        self.assertEqual(
            local_pdfs.normalize_title("A Title, With Punctuation!"),
            local_pdfs.normalize_title("a title with  punctuation"),
        )


class ScanFolderTests(LocalPdfTestBase):
    def test_metadata_and_doi_are_used_and_pdf_is_archived(self):
        make_pdf(
            self.source / "paper one.pdf",
            title="Title From Metadata",
            author="Alice; Bob",
            creation="D:20190501120000Z",
            doi="doi:10.5555/ABC.1",
        )

        papers, stats = local_pdfs.scan_folder(self.source)

        self.assertEqual(len(papers), 1)
        self.assertEqual(stats["scanned"], 1)
        self.assertEqual(stats["copied"] + stats["linked"], 1)
        self.assertEqual(stats["duplicates"], 0)
        self.assertEqual(stats["errors"], 0)

        paper = papers[0]
        self.assertEqual(paper["sources"], ["local"])
        self.assertEqual(paper["title"], "Title From Metadata")
        self.assertEqual(paper["authors"], "Alice; Bob")
        self.assertEqual(paper["doi"], "10.5555/abc.1")
        self.assertEqual(paper["year"], "2019")
        self.assertEqual(paper["pmid"], "")
        self.assertEqual(paper["pmcid"], "")
        self.assertEqual(paper["journal"], "")
        self.assertEqual(paper["publication_type"], "local PDF")
        self.assertTrue(paper["is_oa"])
        self.assertTrue(paper["local_pdf"].startswith("local_"))
        self.assertTrue(paper["local_pdf"].endswith(".pdf"))
        archived = Path(paper["local_pdf_path"])
        self.assertTrue(archived.is_file())
        self.assertEqual(archived.parent, self.archive)
        self.assertTrue(archived.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(paper["local_pdf_original"], str(self.source / "paper one.pdf"))

    def test_filename_title_and_year_are_the_fallback(self):
        make_pdf(self.source / "A_great-paper_2021.pdf")

        papers, _ = local_pdfs.scan_folder(self.source)

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["title"], "A great paper 2021")
        self.assertEqual(papers[0]["year"], "2021")
        self.assertEqual(papers[0]["doi"], "")

    def test_doi_is_read_from_the_first_three_pages(self):
        make_pdf(
            self.source / "text.pdf",
            title="Text Paper",
            page_texts=["", "", "DOI: 10.7777/TEXT.9", "DOI: 10.9999/IGNORED"],
        )

        papers, _ = local_pdfs.scan_folder(self.source)

        self.assertEqual(papers[0]["doi"], "10.7777/text.9")

    def test_recursive_flag_controls_subfolders(self):
        make_pdf(self.source / "top.pdf", title="Top")
        make_pdf(self.source / "nested" / "deep.pdf", title="Deep")

        top_papers, top_stats = local_pdfs.scan_folder(self.source, recursive=False)
        all_papers, all_stats = local_pdfs.scan_folder(self.source, recursive=True)

        self.assertEqual([p["title"] for p in top_papers], ["Top"])
        self.assertEqual(top_stats["scanned"], 1)
        self.assertEqual(
            sorted(p["title"] for p in all_papers), ["Deep", "Top"]
        )
        self.assertEqual(all_stats["scanned"], 2)

    def test_archived_copies_are_skipped(self):
        make_pdf(self.root / "outside.pdf", title="Outside")
        make_pdf(self.archive / "already-archived.pdf", title="Archived")

        papers, stats = local_pdfs.scan_folder(self.root, recursive=True)

        self.assertEqual([p["title"] for p in papers], ["Outside"])
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["scanned"], 1)

    def test_cap_raises_a_clear_error_instead_of_truncating(self):
        for name in ("a", "b", "c"):
            make_pdf(self.source / f"{name}.pdf", title=name.upper())

        with self.assertRaises(ValueError) as raised:
            local_pdfs.scan_folder(self.source, max_files=2)

        self.assertIn("limit is 2", str(raised.exception))
        self.assertEqual(list(self.archive.glob("*.pdf")), [])

    def test_invalid_folders_raise_clear_errors(self):
        with self.assertRaises(ValueError) as blank:
            local_pdfs.scan_folder("")
        self.assertIn("Choose a local PDF folder", str(blank.exception))

        with self.assertRaises(ValueError) as missing:
            local_pdfs.scan_folder(self.root / "missing")
        self.assertIn("does not exist", str(missing.exception))

        not_a_folder = self.root / "file.txt"
        not_a_folder.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError) as wrong_type:
            local_pdfs.scan_folder(not_a_folder)
        self.assertIn("not a folder", str(wrong_type.exception))

    def test_broken_and_encrypted_pdfs_do_not_abort_the_scan(self):
        make_pdf(self.source / "good.pdf", title="Good")
        (self.source / "broken.pdf").write_bytes(b"this is not a pdf")
        make_pdf(self.source / "locked.pdf", title="Locked", encrypt="secret")

        papers, stats = local_pdfs.scan_folder(self.source)

        self.assertEqual([p["title"] for p in papers], ["Good"])
        self.assertEqual(stats["errors"], 2)
        self.assertEqual(len(stats["error_details"]), 2)

    def test_pdf_is_hardlinked_when_the_filesystem_allows_it(self):
        make_pdf(self.source / "paper.pdf", title="Paper")

        with mock.patch.object(
            local_pdfs.shutil, "copy2", side_effect=AssertionError("copy fallback")
        ):
            papers, stats = local_pdfs.scan_folder(self.source)

        self.assertEqual(len(papers), 1)
        self.assertEqual(stats["linked"], 1)
        self.assertEqual(stats["copied"], 0)
        self.assertTrue(Path(papers[0]["local_pdf_path"]).is_file())

    def test_copy_fallback_when_hardlinks_are_unavailable(self):
        make_pdf(self.source / "paper.pdf", title="Paper")

        with mock.patch.object(
            local_pdfs.os, "link", side_effect=OSError("cross-device")
        ):
            papers, stats = local_pdfs.scan_folder(self.source)

        self.assertEqual(len(papers), 1)
        self.assertEqual(stats["copied"], 1)
        self.assertEqual(stats["linked"], 0)
        self.assertTrue(Path(papers[0]["local_pdf_path"]).is_file())

    def test_archived_filename_is_deterministic(self):
        make_pdf(self.source / "paper.pdf", title="Paper")

        first, _ = local_pdfs.scan_folder(self.source)
        second, _ = local_pdfs.scan_folder(self.source)

        self.assertEqual(first[0]["local_pdf"], second[0]["local_pdf"])
        self.assertEqual(first[0]["local_pdf_path"], second[0]["local_pdf_path"])

    def test_local_duplicates_by_doi_are_collapsed(self):
        make_pdf(self.source / "one.pdf", title="First", doi="10.1000/SAME")
        make_pdf(self.source / "two.pdf", title="Second", doi="10.1000/same")

        papers, stats = local_pdfs.scan_folder(self.source)

        self.assertEqual(len(papers), 1)
        self.assertEqual(stats["duplicates"], 1)

    def test_local_duplicates_by_normalized_title_are_collapsed(self):
        make_pdf(self.source / "one.pdf", title="A Study: Of  Things.")
        make_pdf(self.source / "two.pdf", title="a study of things")

        papers, stats = local_pdfs.scan_folder(self.source)

        self.assertEqual(len(papers), 1)
        self.assertEqual(stats["duplicates"], 1)


class MergeLocalPapersTests(unittest.TestCase):
    def _local(self, title, doi="", filename="local_x.pdf"):
        return {
            "sources": ["local"],
            "title": title,
            "authors": "Local Author",
            "doi": doi,
            "year": "2020",
            "journal": "",
            "is_oa": True,
            "local_pdf": filename,
            "local_pdf_path": f"/archive/{filename}",
            "local_pdf_original": f"/source/{filename}",
        }

    def test_doi_match_wins_and_fills_missing_metadata(self):
        online = {
            "paper_key": "pmid:1",
            "sources": ["pubmed"],
            "title": "The Study: Part One",
            "doi": "10.1000/ABC",
            "pmid": "1",
            "year": "2018",
            "journal": "Journal",
            "authors": "",
        }
        local = self._local("the study part one", doi="10.1000/abc")

        merged = local_pdfs.merge_local_papers([online], [local])

        self.assertEqual(len(merged), 1)
        target = merged[0]
        self.assertEqual(target["title"], "The Study: Part One")
        self.assertEqual(target["doi"], "10.1000/ABC")
        self.assertEqual(target["authors"], "Local Author")
        self.assertEqual(target["sources"], ["local", "pubmed"])
        self.assertEqual(target["local_pdf"], "local_x.pdf")
        self.assertEqual(target["local_pdf_original"], "/source/local_x.pdf")

    def test_normalized_title_match_avoids_punctuation_duplicates(self):
        online = {
            "sources": ["crossref"],
            "title": "A Title, With Punctuation!",
            "doi": "",
            "pmid": "",
        }
        local = self._local("a title with  punctuation")

        merged = local_pdfs.merge_local_papers([online], [local])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "A Title, With Punctuation!")
        self.assertIn("local", merged[0]["sources"])

    def test_unmatched_local_pdfs_are_appended(self):
        online = {"sources": ["pubmed"], "title": "Online", "doi": "10.1/online"}
        local = self._local("Local", doi="10.1/local")

        merged = local_pdfs.merge_local_papers([online], [local])

        self.assertEqual([p["title"] for p in merged], ["Online", "Local"])


class IndexTests(LocalPdfTestBase):
    def test_write_load_and_attach_round_trip(self):
        make_pdf(self.source / "paper.pdf", title="Paper", doi="10.1/paper")
        papers, _ = local_pdfs.scan_folder(self.source)
        paper = papers[0]

        index = local_pdfs.write_index(papers)
        key = paper["paper_key"]
        self.assertIn(key, index)
        self.assertEqual(index[key]["filename"], paper["local_pdf"])
        self.assertEqual(index[key]["source"], "local")

        loaded = local_pdfs.load_index()
        self.assertEqual(loaded[key]["original_path"], paper["local_pdf_original"])

        rows = [{"paper_key": key, "title": "Paper", "doi": "10.1/paper"}]
        attached = local_pdfs.attach_to_papers(rows)
        self.assertEqual(attached[0]["local_pdf"], paper["local_pdf"])
        self.assertEqual(attached[0]["local_pdf_path"], paper["local_pdf_path"])

        Path(paper["local_pdf_path"]).unlink()
        fresh = [{"paper_key": key, "title": "Paper"}]
        local_pdfs.attach_to_papers(fresh)
        self.assertNotIn("local_pdf", fresh[0])

    def test_corrupt_or_missing_index_is_ignored(self):
        self.assertEqual(local_pdfs.load_index(), {})
        self.index.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(local_pdfs.load_index(), {})
        self.index.write_text("[]", encoding="utf-8")
        self.assertEqual(local_pdfs.load_index(), {})

    def test_replace_clears_stale_entries_and_merge_keeps_them(self):
        make_pdf(self.source / "one.pdf", title="One", doi="10.1/one")
        make_pdf(self.source / "two.pdf", title="Two", doi="10.1/two")
        papers, _ = local_pdfs.scan_folder(self.source)
        one, two = papers

        local_pdfs.write_index([one])
        kept = local_pdfs.write_index([two], replace=False)
        self.assertIn(one["paper_key"], kept)
        self.assertIn(two["paper_key"], kept)

        replaced = local_pdfs.write_index([two], replace=True)
        self.assertNotIn(one["paper_key"], replaced)
        self.assertIn(two["paper_key"], replaced)

    def test_missing_archived_file_is_not_indexed(self):
        paper = {
            "paper_key": "doi:10.1/missing",
            "title": "Missing",
            "doi": "10.1/missing",
            "local_pdf": "does-not-exist.pdf",
        }
        self.assertEqual(local_pdfs.write_index([paper]), {})
        self.assertEqual(json.loads(self.index.read_text(encoding="utf-8")), {})

    def test_load_search_results_attaches_the_indexed_local_pdf(self):
        from openpyxl import Workbook

        archived = self.archive / "local_saved.pdf"
        make_pdf(archived, title="Saved")
        local_pdfs.save_index({
            "doi:10.1/saved": {
                "filename": archived.name,
                "original_path": "/source/saved.pdf",
                "title": "Saved",
                "source": "local",
            }
        })
        search_xlsx = self.root / "search_results.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(list(main.SEARCH_COLS))
        ws.append([
            "doi:10.1/saved", "local", "Saved", "", "10.1/SAVED", "", "",
            "2020", "", "yes", "",
        ])
        wb.save(search_xlsx)

        with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", search_xlsx):
            papers = main._load_search_results()

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["paper_key"], "doi:10.1/saved")
        self.assertEqual(papers[0]["local_pdf"], "local_saved.pdf")
        self.assertEqual(papers[0]["local_pdf_path"], str(archived))
        self.assertEqual(papers[0]["local_pdf_original"], "/source/saved.pdf")

    def test_existing_local_path_falls_back_to_pdf_dir(self):
        path = self.archive / "fallback.pdf"
        make_pdf(path, title="Fallback")
        paper = {"local_pdf": "fallback.pdf", "local_pdf_path": ""}

        found = local_pdfs.existing_local_path(paper)

        self.assertEqual(found, path)


class PreparePaperTests(LocalPdfTestBase):
    def _local_paper(self, path):
        return {
            "title": "Local Paper",
            "doi": "10.1/local",
            "local_pdf": path.name,
            "local_pdf_path": str(path),
            "local_pdf_original": str(self.source / "paper.pdf"),
            "sources": ["local"],
        }

    def test_local_pdf_uses_local_text_and_skips_the_network(self):
        path = make_pdf(self.archive / "local_paper.pdf", title="Local Paper")
        paper = self._local_paper(path)

        with mock.patch.object(
            main.pdf_fetcher, "pdf_text", lambda p: "x" * (main.fetcher.MIN_TEXT_CHARS + 10)
        ), mock.patch.object(
            main.fetcher, "fetch_full_text", side_effect=AssertionError("network")
        ), mock.patch.object(
            main.pdf_fetcher, "acquire_pdf", side_effect=AssertionError("network")
        ):
            text, source, pdf = main._prepare_paper(paper)

        self.assertEqual(len(text), main.fetcher.MIN_TEXT_CHARS + 10)
        self.assertEqual(source, "pdf:local")
        self.assertEqual(pdf["filename"], "local_paper.pdf")
        self.assertEqual(pdf["label"], "local:folder")
        self.assertEqual(pdf["source"], "local")
        self.assertEqual(pdf["server"], "folder")
        self.assertEqual(pdf["path"], path)

    def test_local_pdf_without_text_does_not_fall_back_to_another_pdf(self):
        path = make_pdf(self.archive / "local_paper.pdf", title="Local Paper")
        paper = self._local_paper(path)

        with mock.patch.object(main.pdf_fetcher, "pdf_text", lambda p: ""), \
             mock.patch.object(
                 main.fetcher, "fetch_full_text", side_effect=AssertionError("network")
             ), \
             mock.patch.object(
                 main.pdf_fetcher, "acquire_pdf", side_effect=AssertionError("network")
             ):
            text, _source, pdf = main._prepare_paper(paper)

        self.assertIsNone(text)
        self.assertEqual(pdf["path"], path)
        self.assertEqual(pdf["label"], "local:folder")

    def test_missing_indexed_file_uses_the_online_path(self):
        paper = {
            "title": "Online Paper",
            "doi": "10.1/online",
            "local_pdf": "missing.pdf",
            "local_pdf_path": str(self.archive / "missing.pdf"),
            "sources": ["pubmed"],
        }
        online_pdf = {
            "path": None, "filename": "", "source": None, "server": "",
            "label": "none", "reason": "no PDF",
        }

        fetch = mock.Mock(return_value=("online text", "pmc"))
        with mock.patch.object(main.fetcher, "fetch_full_text", fetch), \
             mock.patch.object(main.pdf_fetcher, "acquire_pdf", lambda p: online_pdf):
            text, source, pdf = main._prepare_paper(paper)

        self.assertEqual((text, source), ("online text", "pmc"))
        self.assertEqual(pdf, online_pdf)
        fetch.assert_called_once()


class EngineIntegrationTests(LocalPdfTestBase):
    def test_searchers_merge_preserves_local_attachment(self):
        existing = [{"sources": ["pubmed"], "title": "Paper", "doi": "10.1/X"}]
        incoming = [{
            "sources": ["crossref"], "title": "Paper", "doi": "10.1/x",
            "local_pdf": "local.pdf", "local_pdf_path": "/archive/local.pdf",
            "local_pdf_original": "/source/paper.pdf",
        }]

        merged = searchers.merge_papers(existing, incoming)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sources"], ["crossref", "pubmed"])
        self.assertEqual(merged[0]["local_pdf"], "local.pdf")
        self.assertEqual(merged[0]["local_pdf_path"], "/archive/local.pdf")
        self.assertEqual(merged[0]["local_pdf_original"], "/source/paper.pdf")

    def test_cli_local_only_search_skips_online_and_writes_results_and_index(self):
        make_pdf(self.source / "paper.pdf", title="Local Only", doi="10.1000/local")
        search_xlsx = self.root / "search_results.xlsx"
        output = io.StringIO()

        with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", search_xlsx), \
             mock.patch.object(main.config, "EXCLUDED_JOURNALS", []), \
             mock.patch.object(
                 main.searchers, "search_all",
                 side_effect=AssertionError("online search must be skipped"),
             ), \
             contextlib.redirect_stdout(output):
            papers = main.run_search(local_folder=self.source, local_only=True)

        self.assertEqual([p["title"] for p in papers], ["Local Only"])
        self.assertIn("Skipping the online search", output.getvalue())
        self.assertIn("Local PDFs: 1 scanned", output.getvalue())
        from openpyxl import load_workbook
        wb = load_workbook(search_xlsx, read_only=True)
        rows = list(wb.active.iter_rows(values_only=True))
        wb.close()
        self.assertEqual(dict(zip(rows[0], rows[1]))["title"], "Local Only")
        self.assertIn("doi:10.1000/local", local_pdfs.load_index())

    def test_cli_combines_online_and_local_results_without_duplicates(self):
        make_pdf(
            self.source / "paper.pdf",
            title="Shared Paper",
            doi="10.1000/shared",
            creation="D:18890101000000Z",
        )
        search_xlsx = self.root / "search_results.xlsx"
        online = {
            "sources": ["pubmed"],
            "title": "Shared Paper",
            "doi": "10.1000/SHARED",
            "pmid": "42",
            "year": "2020",
            "journal": "Journal",
        }
        calls = []
        output = io.StringIO()

        with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", search_xlsx), \
             mock.patch.object(main.config, "EXCLUDED_JOURNALS", []), \
             mock.patch.object(
                 main.searchers, "search_all",
                 lambda limit=None, year_from=None, year_to=None:
                     calls.append((year_from, year_to)) or [dict(online)],
             ), \
             mock.patch.object(main, "_load_search_results", lambda required=False: []), \
             contextlib.redirect_stdout(output):
            papers = main.run_search(
                year_from="2020", local_folder=self.source
            )

        # Local PDFs are not filtered by the online publication-year range.
        self.assertEqual(calls, [("2020", None)])
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["title"], "Shared Paper")
        self.assertEqual(papers[0]["sources"], ["local", "pubmed"])
        self.assertIn("local_pdf", papers[0])
        self.assertIn("pmid:42", local_pdfs.load_index())
        self.assertIn("Merged local PDFs: 1 matched", output.getvalue())

    def test_local_only_requires_a_folder(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main.parse_args(["--local-only"])

        args = main.parse_args([
            "--local-folder", str(self.source), "--recursive", "--local-only",
        ])
        self.assertEqual(args.local_folder, str(self.source))
        self.assertTrue(args.recursive)
        self.assertTrue(args.local_only)


if __name__ == "__main__":
    unittest.main()
