"""
Unit tests for scraper/letterboxd_scraper.py.

Covers:
- 404 on direct URL triggers search fallback
- Both URLs failing returns None
- Retry count exactly 3 on network failure
"""

import unittest
from unittest.mock import MagicMock, patch

import requests

from scraper.letterboxd_scraper import get_letterboxd_data


def _make_response(html: str = "", status_code: int = 200) -> MagicMock:
    """Helper: build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = html
    return resp


def _make_404(*args, **kwargs) -> MagicMock:
    """Helper: build a 404 mock response."""
    return _make_response(status_code=404)


def _html_with_itemprop_rating(rating: float) -> str:
    """Build a minimal Letterboxd-like page with an itemprop ratingValue meta tag."""
    return f"""
    <html><head>
    <meta itemprop="ratingValue" content="{rating}">
    </head><body><h1>Some Film</h1></body></html>
    """


def _html_with_twitter_rating(rating: float) -> str:
    """Build a minimal page with a twitter:data2 meta tag."""
    return f"""
    <html><head>
    <meta name="twitter:data2" content="{rating} out of 5">
    </head><body></body></html>
    """


def _html_with_average_rating_span(rating: float) -> str:
    """Build a minimal page with a span.average-rating element."""
    return f"""
    <html><body>
    <span class="average-rating">{rating}</span>
    </body></html>
    """


def _html_no_rating() -> str:
    """Build a minimal page with no rating element."""
    return "<html><body><h1>Some Film</h1></body></html>"


def _search_html_with_slug(slug: str) -> str:
    """Build a minimal search results page pointing to a film slug."""
    return f"""
    <html><body>
    <ul class="results">
      <li class="film-detail">
        <a href="/film/{slug}/">
          <h2 class="film-title">Some Film</h2>
        </a>
      </li>
    </ul>
    </body></html>
    """


class TestGetLetterboxdData(unittest.TestCase):

    # ------------------------------------------------------------------
    # 404 on direct URL triggers search fallback
    # ------------------------------------------------------------------
    def test_404_on_direct_url_triggers_search_fallback(self):
        """When all direct slug candidates return 404, the scraper falls back to search."""
        search_html = _search_html_with_slug("some-film")
        movie_html = _html_with_itemprop_rating(3.8)

        responses = [
            _make_404(),                    # some-film -> 404
            _make_404(),                    # some-film-1 -> 404
            _make_404(),                    # some-film-2 -> 404
            _make_response(search_html),    # search page
            _make_response(movie_html),     # film page from search slug
        ]

        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=responses):
            result = get_letterboxd_data("Some Film")

        self.assertIsNotNone(result["rating"])
        self.assertAlmostEqual(result["rating"], 3.8, places=5)

    def test_404_on_direct_url_search_returns_correct_slug(self):
        """Search fallback resolves the correct slug and fetches the film page."""
        search_html = _search_html_with_slug("boogie-nights")
        movie_html = _html_with_itemprop_rating(4.1)

        responses = [
            _make_404(),                    # boogie-nights -> 404
            _make_404(),                    # boogie-nights-1 -> 404
            _make_404(),                    # boogie-nights-2 -> 404
            _make_response(search_html),    # search page
            _make_response(movie_html),     # film page from search slug
        ]

        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=responses):
            result = get_letterboxd_data("Boogie Nights")

        self.assertIsNotNone(result["rating"])
        self.assertAlmostEqual(result["rating"], 4.1, places=5)

    # ------------------------------------------------------------------
    # Both URLs failing returns None
    # ------------------------------------------------------------------
    def test_both_direct_and_search_failing_returns_none(self):
        """When both the direct URL and search fallback fail, rating is None."""
        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=_make_404):
            result = get_letterboxd_data("Nonexistent Movie")

        self.assertIsNone(result["rating"])
        self.assertIsNone(result["rating_count"])
        self.assertIsNone(result["url"])

    def test_direct_404_and_search_returns_no_slug_returns_none(self):
        """When all direct slugs are 404 and search page has no film links, rating is None."""
        empty_search_html = "<html><body><p>No results</p></body></html>"

        responses = [
            _make_404(),                        # some-film -> 404
            _make_404(),                        # some-film-1 -> 404
            _make_404(),                        # some-film-2 -> 404
            _make_response(empty_search_html),  # search page with no links
        ]

        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=responses):
            result = get_letterboxd_data("Totally Unknown Film")

        self.assertIsNone(result["rating"])

    def test_direct_404_and_search_slug_page_also_404_returns_none(self):
        """When all direct slugs are 404, search finds a slug, but that page is also 404."""
        search_html = _search_html_with_slug("some-film")

        responses = [
            _make_404(),                 # some-film -> 404
            _make_404(),                 # some-film-1 -> 404
            _make_404(),                 # some-film-2 -> 404
            _make_response(search_html), # search page
            _make_404(),                 # film page from search slug -> 404
        ]

        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=responses):
            result = get_letterboxd_data("Some Film")

        self.assertIsNone(result["rating"])

    def test_network_error_on_all_attempts_returns_none(self):
        """When all network requests raise exceptions, rating is None."""
        with patch(
            "scraper.letterboxd_scraper.SESSION.get",
            side_effect=requests.ConnectionError("connection refused"),
        ):
            with patch("scraper.letterboxd_scraper.time.sleep"):
                result = get_letterboxd_data("Some Movie")

        self.assertIsNone(result["rating"])

    # ------------------------------------------------------------------
    # Retry count exactly 3 on network failure
    # ------------------------------------------------------------------
    def test_retry_exactly_3_times_on_network_error(self):
        """On persistent network errors, SESSION.get is called exactly 3 times per _fetch."""
        with patch("scraper.letterboxd_scraper.SESSION.get") as mock_get:
            mock_get.side_effect = requests.ConnectionError("connection refused")
            with patch("scraper.letterboxd_scraper.time.sleep"):
                result = get_letterboxd_data("Some Movie")

        # "Some Movie" has 3 slug candidates: some-movie, some-movie-1, some-movie-2
        # Each _fetch retries 3 times = 9 calls for direct slugs.
        # Then search fallback: 3 more calls.
        # Total = 12 calls.
        self.assertEqual(mock_get.call_count, 12)
        self.assertIsNone(result["rating"])

    def test_no_sleep_after_last_retry_attempt(self):
        """Sleep is called between retries but not after the final attempt."""
        with patch("scraper.letterboxd_scraper.SESSION.get") as mock_get:
            mock_get.side_effect = requests.ConnectionError("fail")
            with patch("scraper.letterboxd_scraper.time.sleep") as mock_sleep:
                get_letterboxd_data("Some Movie")

        # With retries=3, sleep is called after attempt 0 and attempt 1 (not after attempt 2).
        # That's 2 sleeps per _fetch call.
        # 4 _fetch calls (3 slug candidates + search) x 2 sleeps = 8 sleeps total.
        self.assertEqual(mock_sleep.call_count, 8)

    def test_sleep_uses_exponential_backoff(self):
        """Sleep durations follow exponential back-off: backoff*1, backoff*2."""
        with patch("scraper.letterboxd_scraper.SESSION.get") as mock_get:
            mock_get.side_effect = requests.ConnectionError("fail")
            with patch("scraper.letterboxd_scraper.time.sleep") as mock_sleep:
                get_letterboxd_data("Some Movie")

        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        # Each _fetch(backoff=2.0) with retries=3 sleeps: 2.0*(2**0)=2.0, 2.0*(2**1)=4.0
        # 4 _fetch calls -> [2.0, 4.0, 2.0, 4.0, 2.0, 4.0, 2.0, 4.0]
        self.assertEqual(len(sleep_args), 8)
        # Within each _fetch pair, durations are strictly increasing
        for i in range(0, len(sleep_args), 2):
            self.assertLess(sleep_args[i], sleep_args[i + 1])

    # ------------------------------------------------------------------
    # Rating extraction from various page structures
    # ------------------------------------------------------------------
    def test_extracts_rating_from_itemprop_meta(self):
        """Extracts rating from <meta itemprop='ratingValue'>."""
        with patch(
            "scraper.letterboxd_scraper.SESSION.get",
            return_value=_make_response(_html_with_itemprop_rating(3.5)),
        ):
            result = get_letterboxd_data("Some Film")

        self.assertAlmostEqual(result["rating"], 3.5, places=5)

    def test_extracts_rating_from_twitter_meta(self):
        """Extracts rating from <meta name='twitter:data2' content='X out of 5'>."""
        with patch(
            "scraper.letterboxd_scraper.SESSION.get",
            return_value=_make_response(_html_with_twitter_rating(3.85)),
        ):
            result = get_letterboxd_data("Some Film")

        self.assertAlmostEqual(result["rating"], 3.85, places=5)

    def test_extracts_rating_from_average_rating_span(self):
        """Extracts rating from <span class='average-rating'>."""
        with patch(
            "scraper.letterboxd_scraper.SESSION.get",
            return_value=_make_response(_html_with_average_rating_span(4.2)),
        ):
            result = get_letterboxd_data("Some Film")

        self.assertAlmostEqual(result["rating"], 4.2, places=5)

    def test_page_with_no_rating_returns_none(self):
        """When the page has no rating element, rating is None."""
        with patch(
            "scraper.letterboxd_scraper.SESSION.get",
            return_value=_make_response(_html_no_rating()),
        ):
            result = get_letterboxd_data("Some Film")

        self.assertIsNone(result["rating"])

    def test_rating_is_clamped_to_0_5_range(self):
        """Rating values outside [0.0, 5.0] are clamped."""
        html = '<html><head><meta itemprop="ratingValue" content="6.0"></head></html>'
        with patch(
            "scraper.letterboxd_scraper.SESSION.get",
            return_value=_make_response(html),
        ):
            result = get_letterboxd_data("Some Film")

        self.assertEqual(result["rating"], 5.0)

    def test_return_dict_has_required_keys(self):
        """Return dict always has 'rating', 'rating_count', and 'url' keys."""
        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=_make_404):
            result = get_letterboxd_data("Some Film")

        self.assertIn("rating", result)
        self.assertIn("rating_count", result)
        self.assertIn("url", result)


if __name__ == "__main__":
    unittest.main()
