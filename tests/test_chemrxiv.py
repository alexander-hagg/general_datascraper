"""ChemRxiv search, preprint marking and workbook migration tests.

All HTTP calls are mocked: ChemRxiv is reached through Crossref metadata only,
so no test touches chemrxiv.org (its public API is behind Cloudflare).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook, load_workbook

import app as web
import config
import main
import searchers
import writer


class FakeResponse:
    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def chemrxiv_item(**overrides):
    item = {
        "DOI": "10.26434/chemrxiv-2024-kw8k6",
        "title": ["Affinity of nicotinoids to a model nAChR binding pocket"],
        "author": [
            {"family": "Santis", "given": "Garrett"},
            {"family": "Okura", "given": "Yuika"},
        ],
        "abstract": "<jats:p>Binding affinity   predicts addiction.</jats:p>",
        "posted": {"date-parts": [[2024, 1, 11]]},
        "published": {"date-parts": [[2024, 1, 11]]},
        "container-title": None,
        "link": [
            {"URL": "https://example.org/landing-page"},
            {
                "URL": "https://chemrxiv.org/doi/pdf/10.26434/chemrxiv-2024-kw8k6",
                "content-type": "application/pdf",
            },
        ],
    }
    item.update(overrides)
    return item


def read_sheet(path):
    wb = load_workbook(path, read_only=True)
    ws = wb.active
    rows = [
        ["" if cell is None else cell for cell in row]
        for row in ws.iter_rows(values_only=True)
    ]
    wb.close()
    return rows


def make_search_workbook(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.append([
        "source", "title", "authors", "doi", "pmid", "pmcid",
        "year", "journal", "is_open_access", "fulltext_url",
    ])
    for row in rows:
        ws.append(row)
    wb.save(path)


class ChemRxivRequestTests(unittest.TestCase):
    def _search(self, items, **kwargs):
        response = FakeResponse({"message": {"items": items}})
        with mock.patch.object(searchers.requests, "get", return_value=response) as get:
            papers = searchers.search_chemrxiv("nAChR", **kwargs)
        return papers, get

    def test_request_uses_crossref_with_prefix_type_and_posted_years(self):
        with mock.patch.object(searchers.config, "NCBI_EMAIL", "test@example.org"):
            papers, get = self._search(
                [chemrxiv_item()], limit=2, year_from=2015, year_to=2020
            )

        self.assertEqual(len(papers), 1)
        get.assert_called_once()
        url = get.call_args.args[0]
        self.assertEqual(url, searchers.CROSSREF)
        self.assertNotIn("chemrxiv.org", url)

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["query"], "nAChR")
        self.assertEqual(params["rows"], "2")
        self.assertEqual(params["mailto"], "test@example.org")
        filters = params["filter"]
        for expected in (
            "type:posted-content",
            "prefix:10.26434",
            "from-posted-date:2015-01-01",
            "until-posted-date:2020-12-31",
        ):
            self.assertIn(expected, filters)
        self.assertEqual(
            params["select"],
            "DOI,title,author,abstract,published,container-title,link,type,"
            "posted,publisher",
        )

    def test_limit_is_applied_to_the_response(self):
        items = [
            chemrxiv_item(DOI="10.26434/chemrxiv-2024-a"),
            chemrxiv_item(DOI="10.26434/chemrxiv-2024-b"),
            chemrxiv_item(DOI="10.26434/chemrxiv-2024-c"),
        ]
        papers, get = self._search(items, limit=2)
        self.assertEqual([p["doi"] for p in papers], [
            "10.26434/chemrxiv-2024-a",
            "10.26434/chemrxiv-2024-b",
        ])
        self.assertEqual(get.call_args.kwargs["params"]["rows"], "2")

    def test_mailto_is_only_sent_when_an_email_is_configured(self):
        with mock.patch.object(searchers.config, "NCBI_EMAIL", ""):
            _, get = self._search([chemrxiv_item()], limit=1)
        self.assertNotIn("mailto", get.call_args.kwargs["params"])

    def test_direct_call_validates_years_before_requesting(self):
        with mock.patch.object(searchers.requests, "get") as get:
            with self.assertRaises(ValueError):
                searchers.search_chemrxiv("A", year_from="15")
        get.assert_not_called()


class ChemRxivMappingTests(unittest.TestCase):
    def _search_one(self, item):
        response = FakeResponse({"message": {"items": [item]}})
        with mock.patch.object(searchers.requests, "get", return_value=response):
            papers = searchers.search_chemrxiv("A", limit=1)
        return papers

    def test_mapping_marks_a_preprint_and_prefers_the_posted_year_and_pdf(self):
        papers = self._search_one(chemrxiv_item())
        self.assertEqual(len(papers), 1)
        paper = papers[0]

        self.assertEqual(paper["sources"], ["chemrxiv"])
        self.assertEqual(paper["publication_type"], "preprint")
        self.assertEqual(
            paper["title"],
            "Affinity of nicotinoids to a model nAChR binding pocket",
        )
        self.assertEqual(paper["authors"], "Santis Garrett; Okura Yuika")
        self.assertEqual(paper["abstract"], "Binding affinity predicts addiction.")
        self.assertEqual(paper["doi"], "10.26434/chemrxiv-2024-kw8k6")
        self.assertEqual(paper["year"], "2024")
        self.assertEqual(paper["journal"], "ChemRxiv")
        self.assertTrue(paper["is_oa"])
        self.assertEqual(
            paper["fulltext_url"],
            "https://chemrxiv.org/doi/pdf/10.26434/chemrxiv-2024-kw8k6",
        )

    def test_year_falls_back_to_published_when_posted_is_missing(self):
        item = chemrxiv_item(
            posted=None,
            published={"date-parts": [[2021, 8, 1]]},
        )
        paper = self._search_one(item)[0]
        self.assertEqual(paper["year"], "2021")

    def test_non_pdf_link_is_the_fallback(self):
        item = chemrxiv_item(link=[{"URL": "https://example.org/landing"}])
        paper = self._search_one(item)[0]
        self.assertEqual(paper["fulltext_url"], "https://example.org/landing")

    def test_non_chemrxiv_dois_under_the_prefix_are_filtered_out(self):
        items = [
            chemrxiv_item(DOI="10.26434/chemrxiv-2024-keep"),
            chemrxiv_item(
                DOI="10.26434/other-2024-drop",
                title=["Not a ChemRxiv posting"],
            ),
        ]
        response = FakeResponse({"message": {"items": items}})
        with mock.patch.object(searchers.requests, "get", return_value=response):
            papers = searchers.search_chemrxiv("A", limit=10)

        self.assertEqual([p["doi"] for p in papers], ["10.26434/chemrxiv-2024-keep"])
        self.assertEqual([p["publication_type"] for p in papers], ["preprint"])

    def test_journal_crossref_mapping_keeps_its_existing_defaults(self):
        paper = searchers._crossref_to_paper({
            "DOI": "10.1000/journal",
            "title": ["A journal paper"],
            "container-title": ["A Journal"],
            "published": {"date-parts": [[2020]]},
            "link": [{"URL": "https://example.org/article"}],
        })
        self.assertEqual(paper["sources"], ["crossref"])
        self.assertEqual(paper["publication_type"], "journal article")
        self.assertEqual(paper["journal"], "A Journal")
        self.assertEqual(paper["year"], "2020")
        self.assertIsNone(paper["is_oa"])

    def test_merge_backfills_publication_type_without_overwriting_it(self):
        merged = searchers.merge_papers(
            [{"sources": ["crossref"], "doi": "10.1/X", "title": "Paper"}],
            [{
                "sources": ["chemrxiv"],
                "doi": "10.1/x",
                "publication_type": "preprint",
            }],
        )
        self.assertEqual(merged[0]["publication_type"], "preprint")
        self.assertEqual(merged[0]["sources"], ["chemrxiv", "crossref"])

        kept = searchers.merge_papers(
            [{
                "sources": ["pubmed"],
                "doi": "10.1/X",
                "publication_type": "journal article",
            }],
            [{"sources": ["chemrxiv"], "doi": "10.1/x",
              "publication_type": "preprint"}],
        )
        self.assertEqual(kept[0]["publication_type"], "journal article")


class RegistrySourceTests(unittest.TestCase):
    def test_registry_and_config_expose_chemrxiv(self):
        self.assertIn("chemrxiv", searchers.SOURCE_REGISTRY)
        self.assertEqual(
            searchers.SOURCE_REGISTRY["chemrxiv"]["label"], "ChemRxiv"
        )
        self.assertIs(
            searchers.SOURCE_REGISTRY["chemrxiv"]["fn"], searchers.search_chemrxiv
        )
        self.assertIn("chemrxiv", config.SOURCES)

    def test_api_sources_lists_chemrxiv(self):
        response = web.app.test_client().get("/api/sources")
        self.assertEqual(response.status_code, 200)
        sources = {item["name"]: item["label"] for item in response.get_json()}
        self.assertEqual(sources.get("chemrxiv"), "ChemRxiv")


class PublicationTypeWorkbookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_search_columns_and_row_carry_publication_type(self):
        self.assertIn("publication_type", main.SEARCH_COLS)
        paper = {
            "sources": ["chemrxiv"],
            "publication_type": "preprint",
            "title": "Preprint",
            "doi": "10.26434/chemrxiv-2024-x",
        }
        row = main.paper_to_row(paper)
        self.assertEqual(row["publication_type"], "preprint")

        path = self.dir / "search_results.xlsx"
        writer.write_xlsx(path, main.SEARCH_COLS, [row])
        rows = read_sheet(path)
        header = rows[0]
        self.assertIn("publication_type", header)
        values = dict(zip(header, rows[1]))
        self.assertEqual(values["publication_type"], "preprint")
        self.assertEqual(values["source"], "chemrxiv")

    def test_extracted_rows_and_header_carry_publication_type(self):
        self.assertIn("publication_type", main.EXTRACT_META_COLUMNS)
        paper = {
            "sources": ["chemrxiv"],
            "publication_type": "preprint",
            "doi": "10.26434/chemrxiv-2024-x",
        }
        rows = main.build_extract_rows(paper, [{"mutation": "M1"}], ["mutation"])
        self.assertEqual(rows[0]["publication_type"], "preprint")

        path = self.dir / "extracted_data.xlsx"
        writer.append_xlsx(path, main.extract_columns(["mutation"]), rows)
        sheet = read_sheet(path)
        header = sheet[0]
        self.assertIn("publication_type", header)
        values = dict(zip(header, sheet[1]))
        self.assertEqual(values["publication_type"], "preprint")
        self.assertEqual(values["mutation"], "M1")

    def test_closed_rows_carry_publication_type(self):
        self.assertIn("publication_type", main.CLOSED_COLS)
        row = main._closed_row(
            {"sources": ["chemrxiv"], "publication_type": "preprint"}, "no PDF"
        )
        self.assertEqual(row["publication_type"], "preprint")

    def test_run_search_preserves_the_chemrxiv_preprint_marker(self):
        path = self.dir / "search_results.xlsx"
        paper = {
            "sources": ["chemrxiv"],
            "publication_type": "preprint",
            "title": "A ChemRxiv preprint",
            "doi": "10.26434/chemrxiv-2024-x",
        }
        with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", path), \
             mock.patch.object(main.config, "EXCLUDED_JOURNALS", []), \
             mock.patch.object(
                 main.searchers, "search_all",
                 lambda limit=None, year_from=None, year_to=None: [paper],
             ), \
             mock.patch.object(main, "_load_search_results", lambda required=False: []):
            main.run_search(year_from="", year_to="")

        rows = read_sheet(path)
        header = rows[0]
        values = dict(zip(header, rows[1]))
        self.assertEqual(values["publication_type"], "preprint")
        self.assertEqual(values["source"], "chemrxiv")


class LegacyWorkbookMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_search_results_migrates_old_header_without_losing_data(self):
        path = self.dir / "search_results.xlsx"
        make_search_workbook(path, [
            ["pubmed", "Old paper", "A", "10.1/OLD", "7", "", "2020", "J", "yes", "u"],
        ])

        with mock.patch.object(main.config, "SEARCH_RESULTS_XLSX", path):
            papers = main._load_search_results()

        self.assertEqual(papers[0]["title"], "Old paper")
        self.assertEqual(papers[0]["publication_type"], "")
        rows = read_sheet(path)
        header = rows[0]
        self.assertIn("publication_type", header)
        values = dict(zip(header, rows[1]))
        self.assertEqual(values["title"], "Old paper")
        self.assertEqual(values["doi"], "10.1/OLD")
        self.assertEqual(values["publication_type"], "")

    def test_extracted_sheet_migration_adds_blank_publication_type(self):
        path = self.dir / "extracted_data.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(["doi", "source", "pdf_file", "pdf_source", "mutation"])
        ws.append(["10.1/OLD", "pubmed", "", "none", "M1"])
        wb.save(path)

        self.assertTrue(
            writer.ensure_columns(path, main.extract_columns(["mutation"]))
        )
        rows = read_sheet(path)
        header = rows[0]
        self.assertIn("publication_type", header)
        values = dict(zip(header, rows[1]))
        self.assertEqual(values["mutation"], "M1")
        self.assertEqual(values["doi"], "10.1/OLD")
        self.assertEqual(values["publication_type"], "")

    def test_results_api_exposes_blank_publication_type_for_legacy_files(self):
        search = self.dir / "search_results.xlsx"
        extracted = self.dir / "extracted_data.xlsx"
        make_search_workbook(search, [
            ["pubmed", "Old paper", "A", "10.1/OLD", "7", "", "2020", "J", "yes", "u"],
        ])
        wb = Workbook()
        ws = wb.active
        ws.append(["doi", "source", "pdf_file", "pdf_source", "mutation"])
        ws.append(["10.1/OLD", "pubmed", "", "none", "M1"])
        wb.save(extracted)

        with mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", search), \
             mock.patch.object(web.config, "EXTRACTED_XLSX", extracted), \
             mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", self.dir / "closed.xlsx"), \
             mock.patch.object(web.config, "PDF_INDEX_JSON", self.dir / "pdf_index.json"):
            payload = web.app.test_client().get("/api/results").get_json()

        for sheet in (payload["search"], payload["extracted"]):
            self.assertIn("publication_type", sheet["header"])
            values = dict(zip(sheet["header"], sheet["rows"][0]))
            self.assertEqual(values["publication_type"], "")


class ReservedColumnTests(unittest.TestCase):
    def test_publication_type_is_reserved_and_protected(self):
        self.assertIn("publication_type", main.RESERVED_COLUMNS)
        self.assertIn("publication_type", main.PROTECTED_COLUMNS)
        cleaned, errors = main.validate_user_columns([
            "mutation", "publication_type", "Publication_Type",
        ])
        self.assertEqual(cleaned, ["mutation"])
        self.assertIn("publication_type", " ".join(errors))

    def test_extract_user_columns_ignores_publication_type(self):
        header = main.extract_columns(["mutation"])
        self.assertEqual(main.extract_user_columns(header), ["mutation"])

    def test_extract_api_rejects_publication_type_column(self):
        client = web.app.test_client()
        response = client.post("/api/extract", json={
            "goal": "Extract mutations",
            "columns": ["publication_type"],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("publication_type", response.get_json()["error"])


class WebMarkupTests(unittest.TestCase):
    def test_app_js_renders_the_type_column_and_preprint_badge(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("publication_type", source)
        self.assertIn("isPreprintGroup", source)
        self.assertIn("data-group-badge-preprint", source)
        self.assertIn('"preprint"', source)
        self.assertIn("search-table", source)

    def test_styles_define_a_visible_preprint_badge(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".data-group-badge-preprint", css)
        self.assertIn(".pub-type-preprint", css)


if __name__ == "__main__":
    unittest.main()
