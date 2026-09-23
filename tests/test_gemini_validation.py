"""
Tests that Gemini-supplied IDs and slugs are checked before their scores are used.

Gemini can invent IMDb IDs (live test: "Without Blood" -> tt14589252, an
episode of a 2021 TV show).  A match is only used when the title matches and
the release year is within tolerance.

Covers:
- titles_match normalisation (case, punctuation, '&', leading article)
- fetch_all's Gemini retry rejects a wrong-title or wrong-year match
- fetch_all's Gemini retry accepts a correct match
- the scrapers' own resolver fallbacks apply the same check
"""

import unittest
from unittest.mock import MagicMock, patch

from scraper.http import titles_match


class TestTitlesMatch(unittest.TestCase):

    def test_case_and_punctuation_ignored(self):
        self.assertTrue(titles_match("Oasis: Don't Look Back In Anger",
                                     "Oasis: Don't Look Back in Anger"))
        self.assertTrue(titles_match("Coyote vs. ACME", "Coyote vs ACME"))

    def test_ampersand_and_article(self):
        self.assertTrue(titles_match("The Fast & the Furious", "Fast and the Furious"))

    def test_different_titles(self):
        self.assertFalse(titles_match("Without Blood", "The Benza"))
        self.assertFalse(titles_match("Ghost in the Shell", "Ghost in the Shell 2: Innocence"))

    def test_missing_title_never_matches(self):
        self.assertFalse(titles_match(None, "Buddy"))
        self.assertFalse(titles_match("", "Buddy"))


class TestFetchAllGeminiRetry(unittest.TestCase):
    """fetch_all pass 2: OMDb found no IMDB rating, so Gemini is asked for an IMDb ID."""

    def _run(self, omdb_by_id, gemini_imdb_id, years=None):
        from update_scores import fetch_all

        resolver = MagicMock()
        resolver.resolve_all_ids.return_value = {
            "metacritic_slug": None, "letterboxd_slug": None, "imdb_id": gemini_imdb_id,
        }
        with patch("update_scores.get_omdb_data",
                   return_value={"metascore": None, "imdb_rating": None, "imdb_id": None, "year": None}), \
             patch("update_scores.get_metacritic_data",
                   return_value={"review_count": 10, "metascore": 41, "year": 2026}), \
             patch("update_scores.get_letterboxd_data",
                   return_value={"rating": 3.02, "year": 2024}), \
             patch("update_scores.get_omdb_data_with_id", side_effect=lambda k, i, **kw: omdb_by_id[i]), \
             patch("update_scores.time.sleep"):
            raw, failed = fetch_all(["Without Blood"], api_key="k", delay=0,
                                    resolver=resolver, years=years)
        return raw[0]

    def test_invented_id_for_another_title_is_rejected(self):
        benza = {"imdb_rating": 8.0, "imdb_id": "tt14589252", "year": 2021, "title": "The Benza"}
        with self.assertLogs("update_scores", level="WARNING") as logs:
            raw = self._run({"tt14589252": benza}, "tt14589252")
        self.assertIsNone(raw.imdb_rating)
        self.assertIsNone(raw.source_years.get("OMDb"))
        self.assertTrue(any("rejected OMDb 'tt14589252'" in m for m in logs.output))

    def test_same_title_wrong_year_is_rejected(self):
        # Other sources agree on 2024-2026, so a 1998 "Without Blood" is a different film.
        old = {"imdb_rating": 5.0, "imdb_id": "tt0000001", "year": 1998, "title": "Without Blood"}
        raw = self._run({"tt0000001": old}, "tt0000001")
        self.assertIsNone(raw.imdb_rating)

    def test_correct_id_is_accepted(self):
        right = {"imdb_rating": 6.1, "imdb_id": "tt18398986", "year": 2024, "title": "Without Blood"}
        raw = self._run({"tt18398986": right}, "tt18398986")
        self.assertEqual(raw.imdb_rating, 6.1)
        self.assertEqual(raw.source_years["OMDb"], 2024)

    def test_user_year_is_the_reference(self):
        right = {"imdb_rating": 6.1, "imdb_id": "tt18398986", "year": 2024, "title": "Without Blood"}
        raw = self._run({"tt18398986": right}, "tt18398986", years={"Without Blood": 1990})
        self.assertIsNone(raw.imdb_rating)


class TestScraperResolverFallbacks(unittest.TestCase):
    """The scrapers' built-in resolver= fallbacks apply the same check."""

    def _resp(self, status=200, text="", json_data=None):
        r = MagicMock()
        r.status_code = status
        r.text = text
        r.json.return_value = json_data
        return r

    def test_letterboxd_rejects_gemini_slug_for_other_film(self):
        from scraper.letterboxd_scraper import get_letterboxd_data
        resolver = MagicMock()
        resolver.resolve_letterboxd_slug.return_value = "some-other-film"
        other = self._resp(text='<meta property="og:title" content="Some Other Film (2001)">'
                                '<meta itemprop="ratingValue" content="4.4">')

        def get(url, **kwargs):
            return other if "some-other-film" in url else self._resp(status=404)

        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=get):
            result = get_letterboxd_data("Without Blood", resolver=resolver)
        self.assertIsNone(result["rating"])

    def test_omdb_rejects_gemini_id_for_other_film(self):
        from scraper.omdb_client import get_omdb_data
        resolver = MagicMock()
        resolver.resolve_imdb_id.return_value = "tt14589252"

        def get(url, params=None, **kwargs):
            if "i" in params:
                return self._resp(json_data={"Response": "True", "Title": "The Benza",
                                             "Year": "2021", "imdbID": "tt14589252",
                                             "imdbRating": "8.0"})
            return self._resp(json_data={"Response": "False", "Error": "Movie not found!"})

        with patch("scraper.omdb_client.SESSION.get", side_effect=get):
            result = get_omdb_data("Without Blood", "k", resolver=resolver)
        self.assertIsNone(result["imdb_rating"])


if __name__ == "__main__":
    unittest.main()
