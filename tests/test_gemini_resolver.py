"""
Unit tests for scraper/gemini_resolver.py and the fetch paths that feed it.

Covers:
- the prompt carries the year and asks only for the wanted identifiers
- JSON replies are parsed even inside ```json fences
- answers are cached; rejected answers aren't reused or re-asked; TTL expiry
- model fallback: unavailable/rate-limited models are skipped for the run,
  timeouts and empty replies fall back for one prompt only
- OMDb search (?s=) picks a real matching title instead of guessing
- fetch_all asks Gemini only about sources that didn't find the film
"""

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import scraper.gemini_resolver as gr
from scraper.gemini_resolver import GeminiResolver, _build_prompt, _parse_json_reply


def _reply(text):
    resp = MagicMock()
    resp.text = text
    resp.candidates = [MagicMock(finish_reason="STOP")]
    return resp


class _FakeClient:
    """Stands in for google.genai.Client; answers via a per-call function."""

    def __init__(self, answer):
        self.calls = []
        self._answer = answer
        self.models = self

    def generate_content(self, model, contents, config=None):
        self.calls.append((model, contents))
        result = self._answer(model, contents)
        if isinstance(result, Exception):
            raise result
        return _reply(result)


class _ResolverTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "cache.json"

    def tearDown(self):
        self._tmp.cleanup()

    def resolver(self, answer):
        r = GeminiResolver(api_key="k", cache_path=self.cache_path)
        r._client = _FakeClient(answer)
        return r


class TestPrompt(unittest.TestCase):

    def test_year_and_only_wanted_keys(self):
        prompt = _build_prompt("Buddy", 2026, ["imdb_id"])
        self.assertIn('"Buddy" released in 2026', prompt)
        self.assertIn('"imdb_id"', prompt)
        self.assertNotIn("metacritic_slug", prompt)
        self.assertIn("Google Search", prompt)

    def test_no_year(self):
        prompt = _build_prompt("Hope", None, ["letterboxd_slug"])
        self.assertIn('"Hope".', prompt)
        self.assertIn("most notable film", prompt)

    def test_parse_json_reply(self):
        self.assertEqual(_parse_json_reply('```json\n{"imdb_id": "tt18398986"}\n```'),
                         {"imdb_id": "tt18398986"})
        self.assertEqual(_parse_json_reply('Sure! {"imdb_id": null}'), {"imdb_id": None})
        self.assertIsNone(_parse_json_reply("no json here"))
        self.assertIsNone(_parse_json_reply(""))


class TestCaching(_ResolverTestCase):

    def test_answer_is_cached_across_instances(self):
        r = self.resolver(lambda m, c: '{"imdb_id": "tt18398986"}')
        self.assertEqual(r.resolve_imdb_id("Without Blood", 2024), "tt18398986")
        self.assertEqual(len(r._client.calls), 1)

        r2 = self.resolver(lambda m, c: self.fail("should not call the API"))
        self.assertEqual(r2.resolve_imdb_id("Without Blood", 2024), "tt18398986")

    def test_cache_key_includes_year(self):
        r = self.resolver(lambda m, c: '{"imdb_id": "tt0000001"}')
        r.resolve_imdb_id("Buddy", 2019)
        r.resolve_imdb_id("Buddy", 2026)
        self.assertEqual(len(r._client.calls), 2)

    def test_only_missing_keys_are_asked(self):
        r = self.resolver(lambda m, c: '{"imdb_id": "tt37281055"}' if '"imdb_id"' in c
                          else '{"letterboxd_slug": "buddy-2026"}')
        r.resolve_all_ids("Buddy", 2026, want=("imdb_id",))
        ids = r.resolve_all_ids("Buddy", 2026, want=("imdb_id", "letterboxd_slug"))
        self.assertEqual(ids["imdb_id"], "tt37281055")
        self.assertEqual(ids["letterboxd_slug"], "buddy-2026")
        self.assertNotIn('"imdb_id"', r._client.calls[1][1])

    def test_rejected_answer_not_reused_or_reasked(self):
        r = self.resolver(lambda m, c: '{"imdb_id": "tt14589252"}')
        self.assertEqual(r.resolve_imdb_id("Without Blood", 2024), "tt14589252")
        r.mark_rejected("Without Blood", 2024, "imdb_id", "tt14589252")
        self.assertIsNone(r.resolve_imdb_id("Without Blood", 2024))
        self.assertEqual(len(r._client.calls), 1)

    def test_null_answer_is_cached(self):
        r = self.resolver(lambda m, c: '{"imdb_id": null}')
        self.assertIsNone(r.resolve_imdb_id("Daayra"))
        self.assertIsNone(r.resolve_imdb_id("Daayra"))
        self.assertEqual(len(r._client.calls), 1)

    def test_expired_entry_is_reasked(self):
        r = self.resolver(lambda m, c: '{"imdb_id": "tt18398986"}')
        r.resolve_imdb_id("Without Blood", 2024)
        data = json.loads(self.cache_path.read_text())
        for entry in data.values():
            entry["ts"] = time.time() - gr._CACHE_TTL_SECONDS - 1
        self.cache_path.write_text(json.dumps(data))

        r2 = self.resolver(lambda m, c: '{"imdb_id": "tt18398986"}')
        r2.resolve_imdb_id("Without Blood", 2024)
        self.assertEqual(len(r2._client.calls), 1)

    def test_failed_request_is_not_cached(self):
        r = self.resolver(lambda m, c: RuntimeError("500 INTERNAL"))
        self.assertIsNone(r.resolve_imdb_id("Buddy", 2026))
        self.assertFalse(self.cache_path.exists())

    def test_corrupt_cache_file_is_ignored(self):
        self.cache_path.write_text("{not json")
        r = self.resolver(lambda m, c: '{"imdb_id": "tt18398986"}')
        self.assertEqual(r.resolve_imdb_id("Without Blood", 2024), "tt18398986")


class TestModelFallback(_ResolverTestCase):

    MODELS = [m[0] for m in gr._GEMINI_MODELS]

    def test_strongest_model_first(self):
        self.assertEqual(self.MODELS[0], "gemini-3-flash-preview")

    def test_unavailable_model_skipped_for_rest_of_run(self):
        def answer(model, contents):
            if model == self.MODELS[0]:
                return RuntimeError("404 NOT_FOUND. This model is no longer available")
            return '{"imdb_id": "tt18398986"}'
        r = self.resolver(answer)
        self.assertEqual(r.resolve_imdb_id("Without Blood", 2024), "tt18398986")
        r.resolve_imdb_id("Buddy", 2026)
        self.assertEqual([m for m, _ in r._client.calls],
                         [self.MODELS[0], self.MODELS[1], self.MODELS[1]])

    def test_timeout_falls_back_for_this_prompt_only(self):
        def answer(model, contents):
            if model == self.MODELS[0] and "Buddy" in contents:
                return RuntimeError("504 DEADLINE_EXCEEDED. Deadline expired")
            return '{"imdb_id": "tt0000001"}'
        r = self.resolver(answer)
        r.resolve_imdb_id("Buddy", 2026)
        r.resolve_imdb_id("Without Blood", 2024)
        self.assertEqual([m for m, _ in r._client.calls],
                         [self.MODELS[0], self.MODELS[1], self.MODELS[0]])

    def test_empty_reply_falls_back(self):
        r = self.resolver(lambda m, c: "" if m == self.MODELS[0] else '{"imdb_id": "tt37281055"}')
        self.assertEqual(r.resolve_imdb_id("Buddy", 2026), "tt37281055")

    def test_other_errors_give_up(self):
        r = self.resolver(lambda m, c: RuntimeError("400 INVALID_ARGUMENT bad request"))
        self.assertIsNone(r.resolve_imdb_id("Buddy", 2026))
        self.assertEqual(len(r._client.calls), 1)

    def test_grounding_is_requested(self):
        r = GeminiResolver(api_key="k")
        self.assertTrue(r._config().tools)
        self.assertFalse(GeminiResolver(api_key="k", grounding=False)._config().tools)


class TestOmdbSearch(unittest.TestCase):

    def _search(self, results, title, year):
        from scraper.omdb_client import _search_imdb_id
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"Response": "True", "Search": results}
        with patch("scraper.omdb_client.SESSION.get", return_value=resp):
            return _search_imdb_id(title, year, "k")

    RESULTS = [
        {"Title": "Buddy", "Year": "1997", "imdbID": "tt0118787"},
        {"Title": "Buddy", "Year": "2026", "imdbID": "tt37281055"},
        {"Title": "Buddy Games", "Year": "2019", "imdbID": "tt9000001"},
    ]

    def test_year_picks_closest_match(self):
        self.assertEqual(self._search(self.RESULTS, "Buddy", 2026), "tt37281055")

    def test_year_with_no_close_match(self):
        self.assertIsNone(self._search(self.RESULTS, "Buddy", 2010))

    def test_several_same_titles_without_year_is_ambiguous(self):
        self.assertIsNone(self._search(self.RESULTS, "Buddy", None))

    def test_single_match_without_year(self):
        self.assertEqual(self._search(self.RESULTS, "Buddy Games", None), "tt9000001")

    def test_get_omdb_data_falls_back_to_search(self):
        from scraper.omdb_client import get_omdb_data

        def get(url, params=None, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "s" in params:
                resp.json.return_value = {"Response": "True", "Search": self.RESULTS}
            elif "i" in params:
                resp.json.return_value = {"Response": "True", "Title": "Buddy", "Year": "2026",
                                          "imdbID": params["i"], "imdbRating": "7.1"}
            else:
                resp.json.return_value = {"Response": "False", "Error": "Movie not found!"}
            return resp

        with patch("scraper.omdb_client.SESSION.get", side_effect=get):
            result = get_omdb_data("Buddy", "k", year=2026)
        self.assertEqual(result["imdb_id"], "tt37281055")
        self.assertEqual(result["imdb_rating"], 7.1)


class TestFetchAllAsksOnlyWhatFailed(unittest.TestCase):

    def _run(self, omdb, mc, lb, years=None):
        from update_scores import fetch_all
        resolver = MagicMock()
        resolver.resolve_all_ids.return_value = {k: None for k in gr.ID_KEYS}
        with patch("update_scores.get_omdb_data", return_value=omdb), \
             patch("update_scores.get_metacritic_data", return_value=mc), \
             patch("update_scores.get_letterboxd_data", return_value=lb), \
             patch("update_scores.time.sleep"):
            fetch_all(["Buddy"], api_key="k", delay=0, resolver=resolver, years=years)
        return resolver

    def test_found_but_unrated_is_not_sent_to_gemini(self):
        # OMDb found the film (has an IMDb ID) but it has no rating yet.
        resolver = self._run(
            omdb={"imdb_id": "tt37281055", "imdb_rating": None, "metascore": None, "year": 2026},
            mc={"review_count": 22, "metascore": 67, "year": 2026, "url": "u"},
            lb={"rating": 3.09, "year": 2026, "url": "u"},
        )
        resolver.resolve_all_ids.assert_not_called()

    def test_only_missing_sources_requested_with_year(self):
        resolver = self._run(
            omdb={"imdb_id": None, "imdb_rating": None, "metascore": None, "year": None},
            mc={"review_count": 22, "metascore": 67, "year": 2026, "url": "u"},
            lb={"rating": None, "year": None, "url": None},
            years={"Buddy": 2026},
        )
        args, kwargs = resolver.resolve_all_ids.call_args
        self.assertEqual(kwargs["year"], 2026)
        self.assertEqual(set(kwargs["want"]), {"imdb_id", "letterboxd_slug"})


if __name__ == "__main__":
    unittest.main()
