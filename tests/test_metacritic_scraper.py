"""
Unit tests for scraper/metacritic_scraper.py.

Covers:
- 404 on direct URL triggers search fallback
- Both URLs failing returns 0
- Page with no review count element returns 0
- Retry count exactly 3 on network failure
"""

import unittest
from unittest.mock import MagicMock, patch, call

import requests
from bs4 import BeautifulSoup

from scraper.metacritic_scraper import get_review_count


def _make_response(html: str = "", status_code: int = 200) -> MagicMock:
    """Helper: build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = html
    return resp


def _make_404(*args, **kwargs) -> MagicMock:
    """Helper: build a 404 mock response. Accepts any args so it can be used as side_effect."""
    return _make_response(status_code=404)


def _html_with_json_ld(review_count: int) -> str:
    """Build a minimal HTML page with JSON-LD containing a review count."""
    return f"""
    <html><head>
    <script type="application/ld+json">
    {{
        "@type": "Movie",
        "aggregateRating": {{
            "ratingValue": "85",
            "reviewCount": {review_count}
        }}
    }}
    </script>
    </head><body></body></html>
    """


def _html_with_html_count(review_count: int) -> str:
    """Build a minimal HTML page with an HTML-based review count."""
    return f"""
    <html><body>
    <span class="count"><a href="#">{review_count} Critic Reviews</a></span>
    </body></html>
    """


def _html_no_review_count() -> str:
    """Build a minimal HTML page with no review count element."""
    return """
    <html><body>
    <h1>Some Movie</h1>
    <p>No review count here.</p>
    </body></html>
    """


class TestGetReviewCount(unittest.TestCase):

    # ------------------------------------------------------------------
    # 404 on direct URL triggers search fallback
    # ------------------------------------------------------------------
    def test_404_on_direct_url_triggers_search_fallback(self):
        """When the direct slug URL returns 404, the scraper falls back to search."""
        # Direct URL returns 404; search returns a slug; slug page has review count
        search_html = """
        <html><body>
        <a href="/movie/some-movie/">Some Movie</a>
        </body></html>
        """
        movie_html = _html_with_json_ld(42)

        responses = [
            _make_404(),           # direct slug (no article)
            _make_response(search_html),  # search page
            _make_response(movie_html),   # movie page from search slug
        ]

        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=responses):
            result = get_review_count("Some Movie")

        self.assertEqual(result, 42)

    def test_404_on_direct_url_with_article_variant_triggers_search(self):
        """When both slug variants (with/without article) return 404, search is used."""
        search_html = """
        <html><body>
        <a href="/movie/the-godfather/">The Godfather</a>
        </body></html>
        """
        movie_html = _html_with_json_ld(100)

        responses = [
            _make_404(),                  # slug without article: "godfather"
            _make_404(),                  # slug with article: "the-godfather"
            _make_response(search_html),  # search page
            _make_response(movie_html),   # movie page from search slug
        ]

        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=responses):
            result = get_review_count("The Godfather")

        self.assertEqual(result, 100)

    # ------------------------------------------------------------------
    # Both URLs failing returns 0
    # ------------------------------------------------------------------
    def test_both_direct_and_search_failing_returns_0(self):
        """When both the direct URL and search fallback fail, returns 0."""
        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=_make_404):
            result = get_review_count("Nonexistent Movie")

        self.assertEqual(result, 0)

    def test_direct_404_and_search_returns_no_slug_returns_0(self):
        """When direct URL is 404 and search page has no movie links, returns 0."""
        empty_search_html = "<html><body><p>No results</p></body></html>"

        responses = [
            _make_404(),                        # direct slug
            _make_response(empty_search_html),  # search page with no links
        ]

        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=responses):
            result = get_review_count("Totally Unknown Film")

        self.assertEqual(result, 0)

    def test_network_error_on_all_attempts_returns_0(self):
        """When all network requests raise exceptions, returns 0."""
        with patch(
            "scraper.metacritic_scraper.SESSION.get",
            side_effect=requests.ConnectionError("connection refused"),
        ):
            with patch("scraper.metacritic_scraper.time.sleep"):
                result = get_review_count("Some Movie")

        self.assertEqual(result, 0)

    # ------------------------------------------------------------------
    # Page with no review count element returns 0
    # ------------------------------------------------------------------
    def test_page_with_no_review_count_element_returns_0(self):
        """When the movie page exists but has no review count, returns 0."""
        # Direct slug returns a page with no count; search also returns nothing useful.
        empty_search_html = "<html><body><p>No results</p></body></html>"
        responses = [
            _make_response(_html_no_review_count()),  # direct slug, no count
            _make_response(empty_search_html),        # search page, no movie links
        ]

        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=responses):
            result = get_review_count("Some Movie")

        self.assertEqual(result, 0)

    def test_page_with_empty_json_ld_returns_0(self):
        """When JSON-LD has no aggregateRating, returns 0."""
        html = """
        <html><head>
        <script type="application/ld+json">{"@type": "Movie", "name": "Test"}</script>
        </head><body></body></html>
        """
        with patch("scraper.metacritic_scraper.SESSION.get", return_value=_make_response(html)):
            result = get_review_count("Test Movie")

        self.assertEqual(result, 0)

    def test_page_with_malformed_json_ld_returns_0(self):
        """When JSON-LD is malformed, falls back to HTML selectors; if none found, returns 0."""
        html = """
        <html><head>
        <script type="application/ld+json">{ this is not valid json }</script>
        </head><body><p>No count here</p></body></html>
        """
        with patch("scraper.metacritic_scraper.SESSION.get", return_value=_make_response(html)):
            result = get_review_count("Test Movie")

        self.assertEqual(result, 0)

    # ------------------------------------------------------------------
    # Retry count exactly 3 on network failure
    # ------------------------------------------------------------------
    def test_retry_exactly_3_times_on_network_error(self):
        """On persistent network errors, SESSION.get is called exactly 3 times per _fetch call."""
        with patch("scraper.metacritic_scraper.SESSION.get") as mock_get:
            mock_get.side_effect = requests.ConnectionError("connection refused")
            with patch("scraper.metacritic_scraper.time.sleep"):
                result = get_review_count("Some Movie")

        # "Some Movie" has no article, so only one slug variant.
        # _fetch is called with retries=3, so 3 attempts for the direct URL.
        # Then search fallback also calls _fetch (3 more attempts).
        # Total = 6 calls (3 for direct + 3 for search).
        self.assertEqual(mock_get.call_count, 6)
        self.assertEqual(result, 0)

    def test_retry_exactly_3_times_per_url_with_article_title(self):
        """With an article title, retries 3 times for each of the two slug variants."""
        with patch("scraper.metacritic_scraper.SESSION.get") as mock_get:
            mock_get.side_effect = requests.ConnectionError("connection refused")
            with patch("scraper.metacritic_scraper.time.sleep"):
                result = get_review_count("The Matrix")

        # "The Matrix" has two slug variants: "matrix" and "the-matrix"
        # Each _fetch call retries 3 times.
        # Direct slugs: 3 + 3 = 6 calls
        # Search fallback: 3 more calls
        # Total = 9 calls
        self.assertEqual(mock_get.call_count, 9)
        self.assertEqual(result, 0)

    def test_no_sleep_after_last_retry_attempt(self):
        """Sleep is called between retries but not after the final attempt."""
        with patch("scraper.metacritic_scraper.SESSION.get") as mock_get:
            mock_get.side_effect = requests.ConnectionError("fail")
            with patch("scraper.metacritic_scraper.time.sleep") as mock_sleep:
                get_review_count("Some Movie")

        # With retries=3, sleep is called after attempt 0 and attempt 1 (not after attempt 2).
        # That's 2 sleeps per _fetch call.
        # "Some Movie" has 1 slug variant → 1 direct _fetch + 1 search _fetch = 4 sleeps total.
        sleep_calls = mock_sleep.call_count
        # Each _fetch with retries=3 sleeps 2 times (between attempts 0→1 and 1→2)
        # 2 _fetch calls (direct + search) × 2 sleeps = 4 sleeps
        self.assertEqual(sleep_calls, 4)

    # ------------------------------------------------------------------
    # Successful extraction from JSON-LD
    # ------------------------------------------------------------------
    def test_extracts_review_count_from_json_ld(self):
        """Successfully extracts review count from JSON-LD structured data."""
        with patch(
            "scraper.metacritic_scraper.SESSION.get",
            return_value=_make_response(_html_with_json_ld(57)),
        ):
            result = get_review_count("Inception")

        self.assertEqual(result, 57)

    # ------------------------------------------------------------------
    # Successful extraction from HTML fallback
    # ------------------------------------------------------------------
    def test_extracts_review_count_from_html_selector(self):
        """Successfully extracts review count from HTML selector when JSON-LD absent."""
        with patch(
            "scraper.metacritic_scraper.SESSION.get",
            return_value=_make_response(_html_with_html_count(33)),
        ):
            result = get_review_count("Parasite")

        self.assertEqual(result, 33)

    # ------------------------------------------------------------------
    # Return type is always int
    # ------------------------------------------------------------------
    def test_return_type_is_int_on_success(self):
        """Return value is always an int."""
        with patch(
            "scraper.metacritic_scraper.SESSION.get",
            return_value=_make_response(_html_with_json_ld(10)),
        ):
            result = get_review_count("Some Film")

        self.assertIsInstance(result, int)

    def test_return_type_is_int_on_failure(self):
        """Return value is int (0) even on failure."""
        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=_make_404):
            result = get_review_count("Unknown Film")

        self.assertIsInstance(result, int)
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
