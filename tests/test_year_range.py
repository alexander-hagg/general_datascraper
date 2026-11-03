"""Tests for the optional publication-year range across all interfaces."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as web
import main
import searchers


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload if payload is not None else {}
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class YearRangeNormalizationTests(unittest.TestCase):
    def test_missing_and_blank_values_mean_no_limit(self):
        self.assertEqual(searchers.normalize_year_range(), (None, None))
        self.assertEqual(searchers.normalize_year_range("", "   "), (None, None))
        self.assertEqual(searchers.normalize_year_range(None, "\n\t"), (None, None))

    def test_integers_and_strings_are_normalized_to_four_digit_strings(self):
        self.assertEqual(
            searchers.normalize_year_range(2015, "2020"),
            ("2015", "2020"),
        )
        self.assertEqual(
            searchers.normalize_year_range(" 1999 ", 2005),
            ("1999", "2005"),
        )
        self.assertEqual(searchers.normalize_year_range("2020", None), ("2020", None))
        self.assertEqual(searchers.normalize_year_range(None, "2020"), (None, "2020"))

    def test_equal_start_and_end_years_are_allowed(self):
        self.assertEqual(
            searchers.normalize_year_range("2020", 2020), ("2020", "2020")
        )

    def test_bools_and_non_four_digit_values_are_rejected(self):
        for value in (True, False, "15", "20155", "abcd", 15, 20155, 2015.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    searchers.normalize_year_range(value, None)
                self.assertIn("four-digit", str(raised.exception))

    def test_inverted_range_is_rejected(self):
        with self.assertRaises(ValueError) as raised:
            searchers.normalize_year_range("2020", "2015")
        self.assertEqual(
            str(raised.exception), "Start year must not be after end year."
        )

    def test_labels_read_well(self):
        self.assertEqual(searchers.year_range_label("2015", "2020"), "2015-2020")
        self.assertEqual(searchers.year_range_label("2015", None), "2015 onwards")
        self.assertEqual(searchers.year_range_label(None, "2020"), "up to 2020")
        self.assertEqual(searchers.year_range_label(None, None), "")


class SourceYearParameterTests(unittest.TestCase):
    def test_pubmed_adds_pdat_bounds_to_esearch(self):
        response = FakeResponse({"esearchresult": {"idlist": []}})
        with mock.patch.object(searchers.requests, "get", return_value=response) as get, \
             mock.patch.object(searchers.time, "sleep", lambda seconds: None):
            self.assertEqual(
                searchers.search_pubmed("A", limit=5, year_from=2015, year_to=2020),
                [],
            )

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["datetype"], "pdat")
        self.assertEqual(params["mindate"], "2015")
        self.assertEqual(params["maxdate"], "2020")
        self.assertEqual(params["term"], "A")

    def test_pubmed_open_ends_are_omitted(self):
        response = FakeResponse({"esearchresult": {"idlist": []}})
        with mock.patch.object(searchers.requests, "get", return_value=response) as get, \
             mock.patch.object(searchers.time, "sleep", lambda seconds: None):
            searchers.search_pubmed("A", limit=5, year_from=2015)

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["datetype"], "pdat")
        self.assertEqual(params["mindate"], "2015")
        self.assertNotIn("maxdate", params)

    def test_pubmed_without_bounds_sends_no_date_parameters(self):
        response = FakeResponse({"esearchresult": {"idlist": []}})
        with mock.patch.object(searchers.requests, "get", return_value=response) as get, \
             mock.patch.object(searchers.time, "sleep", lambda seconds: None):
            searchers.search_pubmed("A", limit=5)

        params = get.call_args.kwargs["params"]
        for key in ("datetype", "mindate", "maxdate"):
            self.assertNotIn(key, params)

    def test_europepmc_appends_a_pub_year_clause_and_trims_the_base_query(self):
        response = FakeResponse({"resultList": {"result": []}})
        with mock.patch.object(searchers.requests, "get", return_value=response) as get:
            searchers.search_europepmc(
                "A AND B   \n", limit=5, year_from="2015", year_to="2020"
            )

        query = get.call_args.kwargs["params"]["query"]
        self.assertEqual(query, "A AND B AND PUB_YEAR:[2015 TO 2020]")

    def test_europepmc_uses_open_bounds_for_missing_sides(self):
        response = FakeResponse({"resultList": {"result": []}})
        cases = [
            (("2015", None), "PUB_YEAR:[2015 TO 3000]"),
            ((None, "2020"), "PUB_YEAR:[1000 TO 2020]"),
        ]
        for years, clause in cases:
            with self.subTest(years=years):
                with mock.patch.object(searchers.requests, "get", return_value=response) as get:
                    searchers.search_europepmc("A", limit=5, year_from=years[0], year_to=years[1])
                self.assertEqual(get.call_args.kwargs["params"]["query"], f"A AND {clause}")

    def test_crossref_appends_date_filters_alongside_the_article_filter(self):
        response = FakeResponse({"message": {"items": []}})
        with mock.patch.object(searchers.requests, "get", return_value=response) as get:
            searchers.search_crossref("A", limit=5, year_from=2015, year_to=2020)

        filters = get.call_args.kwargs["params"]["filter"]
        self.assertEqual(
            filters,
            "type:journal-article,"
            "from-pub-date:2015-01-01,until-pub-date:2020-12-31",
        )

    def test_crossref_open_end_keeps_the_article_filter(self):
        response = FakeResponse({"message": {"items": []}})
        with mock.patch.object(searchers.requests, "get", return_value=response) as get:
            searchers.search_crossref("A", limit=5, year_to="2020")

        filters = get.call_args.kwargs["params"]["filter"]
        self.assertEqual(
            filters, "type:journal-article,until-pub-date:2020-12-31"
        )

    def test_direct_source_calls_validate_before_making_requests(self):
        with mock.patch.object(searchers.requests, "get") as get:
            with self.assertRaises(ValueError):
                searchers.search_pubmed("A", year_from="15")
            with self.assertRaises(ValueError):
                searchers.search_europepmc("A", year_to="2020", year_from="2021")
            with self.assertRaises(ValueError):
                searchers.search_crossref("A", year_from=True)
        get.assert_not_called()


class SearchForwardingTests(unittest.TestCase):
    def test_search_normalizes_once_and_forwards_to_sources(self):
        calls = []

        def fake_source(query, limit=None, year_from=None, year_to=None):
            calls.append((query, limit, year_from, year_to))
            return [{"sources": ["fake"], "title": "Paper", "doi": "10.1/X"}]

        registry = {"fake": {"label": "Fake", "fn": fake_source}}
        with mock.patch.object(searchers, "SOURCE_REGISTRY", registry):
            papers = searchers.search(
                "A", ["fake"], limit=7, year_from=2015, year_to="2020"
            )

        self.assertEqual(len(papers), 1)
        self.assertEqual(calls, [("A", 7, "2015", "2020")])

    def test_search_rejects_invalid_ranges_before_searching(self):
        calls = []
        registry = {
            "fake": {
                "label": "Fake",
                "fn": lambda *a, **k: calls.append((a, k)) or [],
            }
        }
        with mock.patch.object(searchers, "SOURCE_REGISTRY", registry):
            with self.assertRaises(ValueError):
                searchers.search("A", ["fake"], year_from="2020", year_to="2015")
        self.assertEqual(calls, [])

    def test_search_still_dedupes_and_merges_sources(self):
        def one(query, limit=None, year_from=None, year_to=None):
            return [{"sources": ["one"], "title": "Paper", "doi": "10.1/X"}]

        def two(query, limit=None, year_from=None, year_to=None):
            return [{
                "sources": ["two"], "title": "Paper", "doi": "10.1/x",
                "abstract": "From source two",
            }]

        registry = {
            "one": {"label": "One", "fn": one},
            "two": {"label": "Two", "fn": two},
        }
        with mock.patch.object(searchers, "SOURCE_REGISTRY", registry):
            papers = searchers.search("A", ["one", "two"], year_from=2015)

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["sources"], ["one", "two"])
        self.assertEqual(papers[0]["abstract"], "From source two")

    def test_search_all_forwards_normalized_bounds_to_each_query(self):
        calls = []

        def fake_search(query, sources, limit=None, year_from=None, year_to=None):
            calls.append((query, sources, limit, year_from, year_to))
            return [{"sources": ["fake"], "title": query}]

        with mock.patch.object(searchers.config, "QUERIES", ["Q1", "Q2"]), \
             mock.patch.object(searchers.config, "SOURCES", ["pubmed"]), \
             mock.patch.object(searchers, "search", fake_search):
            papers = searchers.search_all(limit=3, year_from=2015, year_to="2020")

        self.assertEqual([p["title"] for p in papers], ["Q1", "Q2"])
        self.assertEqual(calls, [
            ("Q1", ["pubmed"], 3, "2015", "2020"),
            ("Q2", ["pubmed"], 3, "2015", "2020"),
        ])

    def test_search_raises_cancelled_between_sources(self):
        calls = []
        state = {"cancel": False}

        def first(query, limit=None, year_from=None, year_to=None):
            calls.append("first")
            state["cancel"] = True
            return [{"sources": ["first"], "title": "One"}]

        def second(query, limit=None, year_from=None, year_to=None):
            calls.append("second")
            return [{"sources": ["second"], "title": "Two"}]

        registry = {
            "first": {"label": "First", "fn": first},
            "second": {"label": "Second", "fn": second},
        }
        with mock.patch.object(searchers, "SOURCE_REGISTRY", registry):
            with self.assertRaises(searchers.SearchCancelled):
                searchers.search(
                    "A", ["first", "second"],
                    cancel_check=lambda: state["cancel"],
                )

        self.assertEqual(calls, ["first"])

    def test_search_all_forwards_and_honours_cancel_check(self):
        calls = []
        state = {"cancel": False}

        def fake_search(query, sources, limit=None, year_from=None, year_to=None,
                        cancel_check=None):
            calls.append((query, cancel_check))
            state["cancel"] = True
            return [{"sources": ["fake"], "title": query}]

        def cancel_check():
            return state["cancel"]

        with mock.patch.object(searchers.config, "QUERIES", ["Q1", "Q2"]), \
             mock.patch.object(searchers.config, "SOURCES", ["pubmed"]), \
             mock.patch.object(searchers, "search", fake_search):
            with self.assertRaises(searchers.SearchCancelled):
                searchers.search_all(
                    limit=3, year_from=2015, year_to="2020",
                    cancel_check=cancel_check,
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "Q1")
        self.assertIs(calls[0][1], cancel_check)

    def test_search_without_cancel_check_keeps_existing_behavior(self):
        def one(query, limit=None, year_from=None, year_to=None):
            return [{"sources": ["one"], "title": "One", "doi": "10.1/X"}]

        registry = {"one": {"label": "One", "fn": one}}
        with mock.patch.object(searchers, "SOURCE_REGISTRY", registry):
            papers = searchers.search("A", ["one"])
        self.assertEqual([p["title"] for p in papers], ["One"])


class MainYearRangeTests(unittest.TestCase):
    def _run_search(self, config_from=None, config_to=None, **kwargs):
        calls = []

        def fake_search_all(limit=None, year_from=None, year_to=None):
            calls.append((limit, year_from, year_to))
            return []

        output = io.StringIO()
        with mock.patch.object(main.config, "YEAR_FROM", config_from), \
             mock.patch.object(main.config, "YEAR_TO", config_to), \
             mock.patch.object(main.config, "EXCLUDED_JOURNALS", []), \
             mock.patch.object(main.searchers, "search_all", fake_search_all), \
             mock.patch.object(main, "_load_search_results", lambda required=False: []), \
             mock.patch.object(main.writer, "write_xlsx", lambda *a, **k: None), \
             contextlib.redirect_stdout(output):
            papers = main.run_search(**kwargs)
        return papers, calls, output.getvalue()

    def test_run_search_uses_config_defaults(self):
        papers, calls, output = self._run_search(
            config_from=2015, config_to=2020
        )
        self.assertEqual(papers, [])
        self.assertEqual(calls, [(None, "2015", "2020")])
        self.assertIn("Publication years: 2015-2020", output)

    def test_run_search_arguments_override_config(self):
        _, calls, _ = self._run_search(
            config_from=1990, config_to=1995, year_from=2015, year_to="2020"
        )
        self.assertEqual(calls, [(None, "2015", "2020")])

    def test_explicit_blanks_override_configured_years(self):
        _, calls, output = self._run_search(
            config_from=2015, config_to=2020, year_from="", year_to=""
        )
        self.assertEqual(calls, [(None, None, None)])
        self.assertNotIn("Publication years:", output)

    def test_invalid_range_raises_before_searching(self):
        calls = []
        with mock.patch.object(
            main.searchers, "search_all",
            lambda **kwargs: calls.append(kwargs) or [],
        ):
            with self.assertRaises(ValueError):
                main.run_search(year_from="2020", year_to="2015")
        self.assertEqual(calls, [])

    def test_cli_year_arguments_are_parsed(self):
        args = main.parse_args([
            "--search-only", "--year-from", "2015", "--year-to", "2020",
        ])
        self.assertEqual(args.year_from, "2015")
        self.assertEqual(args.year_to, "2020")

    def test_cli_rejects_a_malformed_year_cleanly(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main.parse_args(["--year-from", "15"])

    def test_cli_rejects_an_inverted_range_cleanly(self):
        with mock.patch.object(
            main.searchers, "search_all",
            side_effect=AssertionError("search must not start"),
        ), mock.patch.object(
            sys, "argv",
            ["main.py", "--search-only", "--year-from", "2020", "--year-to", "2015"],
        ), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main.main()
        self.assertIn("Start year must not be after end year", str(raised.exception))


class SearchJobYearRangeTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(web.history, "record")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _job(self):
        return {
            "log": [],
            "progress": {"done": 0, "total": 0, "current": ""},
            "result": None,
            "status": "running",
            "error": None,
        }

    def _paper(self):
        return {"sources": ["pubmed"], "title": "Paper", "doi": "10.1/X"}

    def _run_job(self, keywords, saved_settings=None, **kwargs):
        calls = []

        def fake_search(query, sources, limit=None, year_from=None, year_to=None,
                        cancel_check=None):
            calls.append((query, sources, limit, year_from, year_to))
            return [self._paper()]

        job = self._job()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(web.config, "SEARCH_RESULTS_XLSX", tmp / "search.xlsx"), \
                 mock.patch.object(web.config, "EXTRACTED_XLSX", tmp / "extracted.xlsx"), \
                 mock.patch.object(web.config, "CLOSED_SOURCE_XLSX", tmp / "closed.xlsx"), \
                 mock.patch.object(web.config, "CHECKPOINT_JSON", tmp / "checkpoint.json"), \
                 mock.patch.object(
                     web.config, "LOCAL_PDF_INDEX_JSON", tmp / "local_pdfs.json"
                 ), \
                 mock.patch.object(web.config, "FIGURES_DIR", tmp / "figures"), \
                 mock.patch.object(
                     web.config, "FIGURES_INDEX_JSON", tmp / "figures" / "index.json"
                 ), \
                 mock.patch.object(web.config, "FIGURES_XLSX", tmp / "figures.xlsx"), \
                 mock.patch.object(web, "apply_settings", lambda s: None), \
                 mock.patch.object(
                     web, "load_settings",
                     lambda: dict(saved_settings or keywords.get("saved", {})),
                 ), \
                 mock.patch.object(web.searchers, "search", fake_search):
                web._run_search_job(job, "A", ["pubmed"], 10, **kwargs)
        return job, calls

    def test_job_logs_forwards_and_reports_the_range(self):
        job, calls = self._run_job(
            {}, year_from="2015", year_to="2020"
        )
        self.assertEqual(job["status"], "done")
        self.assertEqual(calls, [("A", ["pubmed"], 10, "2015", "2020")])
        self.assertEqual(job["result"]["year_from"], "2015")
        self.assertEqual(job["result"]["year_to"], "2020")
        self.assertEqual(job["result"]["year_range"], "2015-2020")
        self.assertIn("Publication years: 2015-2020", "\n".join(job["log"]))
        self.assertIn("years 2015-2020", "\n".join(job["log"]))
        # Existing response keys are still present.
        for key in ("papers", "added", "total", "excluded"):
            self.assertIn(key, job["result"])

    def test_omitted_bounds_fall_back_to_saved_settings(self):
        job, calls = self._run_job(
            {}, saved_settings={"year_from": "2015", "year_to": "2020"}
        )
        self.assertEqual(job["status"], "done")
        self.assertEqual(calls, [("A", ["pubmed"], 10, "2015", "2020")])

    def test_explicit_blank_does_not_fall_back_to_saved_settings(self):
        job, calls = self._run_job(
            {},
            saved_settings={"year_from": "2015", "year_to": "2020"},
            year_from="",
            year_to="",
        )
        self.assertEqual(job["status"], "done")
        self.assertEqual(calls, [("A", ["pubmed"], 10, None, None)])
        self.assertEqual(job["result"]["year_range"], "all years")

    def test_explicit_none_bounds_do_not_fall_back_to_saved_settings(self):
        # /api/search normalizes blank inputs to None before starting the job.
        job, calls = self._run_job(
            {},
            saved_settings={"year_from": "2015", "year_to": "2020"},
            year_from=None,
            year_to=None,
        )
        self.assertEqual(job["status"], "done")
        self.assertEqual(calls, [("A", ["pubmed"], 10, None, None)])
        self.assertEqual(job["result"]["year_range"], "all years")

    def test_one_explicit_bound_keeps_the_other_side_open(self):
        _, calls = self._run_job(
            {},
            saved_settings={"year_from": "2015", "year_to": "2020"},
            year_from="2018",
            year_to=None,
        )
        self.assertEqual(calls, [("A", ["pubmed"], 10, "2018", None)])

    def test_invalid_range_marks_the_job_as_an_error_without_searching(self):
        job = self._job()
        with mock.patch.object(web, "apply_settings", lambda s: None), \
             mock.patch.object(web, "load_settings", lambda: {}), \
             mock.patch.object(web.searchers, "search") as search:
            web._run_search_job(
                job, "A", ["pubmed"], 10, year_from="2020", year_to="2015"
            )
        self.assertEqual(job["status"], "error")
        self.assertIn("Start year must not be after end year", job["error"])
        search.assert_not_called()


class SearchApiYearRangeTests(unittest.TestCase):
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
             mock.patch.object(web, "SETTINGS_PATH", self.settings_path), \
             mock.patch.object(web.config, "EXCLUDED_JOURNALS", []), \
             mock.patch.object(web.config, "YEAR_FROM", None), \
             mock.patch.object(web.config, "YEAR_TO", None):
            return self.client.post("/api/search", json=payload)

    def test_string_and_numeric_years_are_forwarded_and_persisted(self):
        response = self._post({
            "query": "A AND B",
            "sources": ["pubmed"],
            "max_results": 5,
            "year_from": 2015,
            "year_to": " 2020 ",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.calls[0][2],
            ("A AND B", ["pubmed"], 5, [], "2015", "2020", "", False, None),
        )
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["year_from"], "2015")
        self.assertEqual(saved["year_to"], "2020")

    def test_old_client_falls_back_to_saved_years(self):
        self.settings_path.write_text(
            json.dumps({"year_from": "2015", "year_to": "2020"}),
            encoding="utf-8",
        )
        response = self._post({
            "query": "A", "sources": ["crossref"], "max_results": 3,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.calls[0][2],
            ("A", ["crossref"], 3, [], "2015", "2020", "", False, None),
        )

    def test_explicit_blank_clears_the_saved_range(self):
        self.settings_path.write_text(
            json.dumps({"year_from": "2015", "year_to": "2020"}),
            encoding="utf-8",
        )
        response = self._post({
            "query": "A", "sources": ["crossref"], "year_from": "", "year_to": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.calls[0][2],
            ("A", ["crossref"], 50, [], None, None, "", False, None),
        )
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertIsNone(saved["year_from"])
        self.assertIsNone(saved["year_to"])

    def test_malformed_year_gets_400_before_a_job_starts(self):
        response = self._post({
            "query": "A", "sources": ["pubmed"], "year_from": "15",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("four-digit", response.get_json()["error"])
        self.assertEqual(self.calls, [])

    def test_inverted_range_gets_400_before_a_job_starts(self):
        response = self._post({
            "query": "A", "sources": ["pubmed"],
            "year_from": "2020", "year_to": "2015",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "Start year must not be after end year",
            response.get_json()["error"],
        )
        self.assertEqual(self.calls, [])


class SettingsApiYearRangeTests(unittest.TestCase):
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

    def test_years_round_trip_as_normalized_strings(self):
        response = self._request("POST", {"year_from": 2015, "year_to": "2020"})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["year_from"], "2015")
        self.assertEqual(body["year_to"], "2020")

        fetched = self._request("GET").get_json()
        self.assertEqual(fetched["year_from"], "2015")
        self.assertEqual(fetched["year_to"], "2020")

    def test_blank_years_clear_the_saved_range(self):
        self._request("POST", {"year_from": "2015", "year_to": "2020"})
        response = self._request("POST", {"year_from": "", "year_to": None})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["year_from"])
        self.assertIsNone(response.get_json()["year_to"])

    def test_partial_update_keeps_the_other_saved_bound(self):
        self._request("POST", {"year_from": "2015", "year_to": "2020"})
        response = self._request("POST", {"year_from": "2016"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["year_from"], "2016")
        self.assertEqual(response.get_json()["year_to"], "2020")

    def test_malformed_year_gets_400(self):
        response = self._request("POST", {"year_from": "15"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("four-digit", response.get_json()["error"])

    def test_inverted_range_gets_400(self):
        response = self._request("POST", {
            "year_from": "2020", "year_to": "2015",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "Start year must not be after end year",
            response.get_json()["error"],
        )


if __name__ == "__main__":
    unittest.main()
