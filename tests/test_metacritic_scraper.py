"""
Unit tests for scraper/metacritic_scraper.py.

Covers:
- 404 on direct URL triggers search fallback
- Both URLs failing returns 0 review_count and None metascore
- Page with no review count element returns 0
- Retry count exactly 3 on network failure
- get_metacritic_data returns metascore from JSON-LD for 4+ reviews
- get_metacritic_data averages individual scores for 1–3 reviews
- get_review_count backward-compat wrapper still works
"""

import unittest
from unittest.mock import MagicMock, patch, call

import requests
from bs4 import BeautifulSoup

from scraper.metacritic_scraper import get_review_count, get_metacritic_data


def _make_response(html: str = "", status_code: int = 200) -> MagicMock:
    """Helper: build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = html
    return resp


def _make_404(*args, **kwargs) -> MagicMock:
    """Helper: build a 404 mock response. Accepts any args so it can be used as side_effect."""
    return _make_response(status_code=404)


def _html_with_json_ld(review_count: int, rating_value: str = "85") -> str:
    """Build a minimal HTML page with JSON-LD containing a review count and aggregate score."""
    return f"""
    <html><head>
    <script type="application/ld+json">
    {{
        "@type": "Movie",
        "aggregateRating": {{
            "ratingValue": "{rating_value}",
            "reviewCount": {review_count}
        }}
    }}
    </script>
    </head><body></body></html>
    """


def _html_with_json_ld_no_score(review_count: int) -> str:
    """Build a minimal HTML page with JSON-LD that has a review count but no ratingValue."""
    return f"""
    <html><head>
    <script type="application/ld+json">
    {{
        "@type": "Movie",
        "aggregateRating": {{
            "reviewCount": {review_count}
        }}
    }}
    </script>
    </head><body></body></html>
    """


def _html_reviews_page(scores: list) -> str:
    """Build a minimal critic-reviews page with individual score entries."""
    items = "\n".join(
        f'<span>Metascore {s} out of 100 Some Publication</span>'
        for s in scores
    )
    return f"<html><body>{items}</body></html>"


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


class TestGetMetacriticData(unittest.TestCase):
    """Tests for the get_metacritic_data function."""

    # ------------------------------------------------------------------
    # 4+ reviews: aggregate score returned from JSON-LD
    # ------------------------------------------------------------------
    def test_returns_aggregate_score_for_many_reviews(self):
        """With 4+ reviews, metascore is read from JSON-LD aggregateRating."""
        with patch(
            "scraper.metacritic_scraper.SESSION.get",
            return_value=_make_response(_html_with_json_ld(10, rating_value="78")),
        ):
            result = get_metacritic_data("Inception")

        self.assertEqual(result["review_count"], 10)
        self.assertEqual(result["metascore"], 78)

    def test_aggregate_score_clamped_to_0_100(self):
        """Aggregate score is clamped to the 0–100 range."""
        with patch(
            "scraper.metacritic_scraper.SESSION.get",
            return_value=_make_response(_html_with_json_ld(5, rating_value="105")),
        ):
            result = get_metacritic_data("Some Film")

        self.assertLessEqual(result["metascore"], 100)

    # ------------------------------------------------------------------
    # 1–3 reviews: individual scores averaged from critic-reviews page
    # ------------------------------------------------------------------
    def test_averages_individual_scores_for_one_review(self):
        """With 1 review, fetches critic-reviews page and returns that single score."""
        main_html = _html_with_json_ld_no_score(1)
        reviews_html = _html_reviews_page([72])

        responses = [
            _make_response(main_html),    # main movie page
            _make_response(reviews_html), # critic-reviews sub-page
        ]

        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=responses):
            result = get_metacritic_data("Rare Film")

        self.assertEqual(result["review_count"], 1)
        self.assertEqual(result["metascore"], 72)

    def test_averages_individual_scores_for_two_reviews(self):
        """With 2 reviews, averages the two individual scores."""
        main_html = _html_with_json_ld_no_score(2)
        reviews_html = _html_reviews_page([80, 60])  # average = 70

        responses = [
            _make_response(main_html),
            _make_response(reviews_html),
        ]

        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=responses):
            result = get_metacritic_data("Small Film")

        self.assertEqual(result["review_count"], 2)
        self.assertEqual(result["metascore"], 70)

    def test_averages_individual_scores_for_three_reviews(self):
        """With 3 reviews, averages all three individual scores."""
        main_html = _html_with_json_ld_no_score(3)
        reviews_html = _html_reviews_page([90, 80, 70])  # average = 80

        responses = [
            _make_response(main_html),
            _make_response(reviews_html),
        ]

        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=responses):
            result = get_metacritic_data("Tiny Release")

        self.assertEqual(result["review_count"], 3)
        self.assertEqual(result["metascore"], 80)

    def test_rounds_average_to_nearest_int(self):
        """Averaged score is rounded to the nearest integer."""
        main_html = _html_with_json_ld_no_score(2)
        reviews_html = _html_reviews_page([75, 76])  # average = 75.5 → rounds to 76

        responses = [
            _make_response(main_html),
            _make_response(reviews_html),
        ]

        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=responses):
            result = get_metacritic_data("Some Film")

        self.assertEqual(result["metascore"], 76)

    def test_metascore_none_when_reviews_page_has_no_scores(self):
        """When the critic-reviews page has no parseable scores, metascore is None."""
        main_html = _html_with_json_ld_no_score(2)
        reviews_html = "<html><body><p>No scores here</p></body></html>"

        responses = [
            _make_response(main_html),
            _make_response(reviews_html),
        ]

        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=responses):
            result = get_metacritic_data("Mystery Film")

        self.assertEqual(result["review_count"], 2)
        self.assertIsNone(result["metascore"])

    def test_metascore_none_when_reviews_page_404(self):
        """When the critic-reviews sub-page returns 404, metascore is None."""
        main_html = _html_with_json_ld_no_score(1)

        responses = [
            _make_response(main_html),  # main page
            _make_404(),                # critic-reviews page
        ]

        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=responses):
            result = get_metacritic_data("Obscure Film")

        self.assertEqual(result["review_count"], 1)
        self.assertIsNone(result["metascore"])

    # ------------------------------------------------------------------
    # Zero reviews: metascore is None, no reviews page fetched
    # ------------------------------------------------------------------
    def test_zero_reviews_returns_none_metascore(self):
        """When review_count is 0, metascore is None and no reviews page is fetched."""
        html = "<html><body><p>No reviews yet</p></body></html>"

        with patch("scraper.metacritic_scraper.SESSION.get") as mock_get:
            mock_get.return_value = _make_response(html)
            result = get_metacritic_data("Unreleased Film")

        self.assertEqual(result["review_count"], 0)
        self.assertIsNone(result["metascore"])
        # Only the main page should have been fetched (no reviews sub-page)
        self.assertEqual(mock_get.call_count, 1)

    # ------------------------------------------------------------------
    # Not found: both review_count 0 and metascore None
    # ------------------------------------------------------------------
    def test_not_found_returns_zero_count_and_none_score(self):
        """When the movie page is not found, returns review_count=0 and metascore=None."""
        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=_make_404):
            result = get_metacritic_data("Nonexistent Movie")

        self.assertEqual(result["review_count"], 0)
        self.assertIsNone(result["metascore"])

    # ------------------------------------------------------------------
    # Return shape
    # ------------------------------------------------------------------
    def test_return_dict_has_required_keys(self):
        """Return value always has review_count and metascore keys."""
        with patch("scraper.metacritic_scraper.SESSION.get", side_effect=_make_404):
            result = get_metacritic_data("Any Film")

        self.assertIn("review_count", result)
        self.assertIn("metascore", result)

    def test_review_count_is_always_int(self):
        """review_count is always an int."""
        with patch(
            "scraper.metacritic_scraper.SESSION.get",
            return_value=_make_response(_html_with_json_ld(7)),
        ):
            result = get_metacritic_data("Some Film")

        self.assertIsInstance(result["review_count"], int)

    # ------------------------------------------------------------------
    # Backward-compat wrapper
    # ------------------------------------------------------------------
    def test_get_review_count_wrapper_returns_int(self):
        """get_review_count still returns an int (backward compat)."""
        with patch(
            "scraper.metacritic_scraper.SESSION.get",
            return_value=_make_response(_html_with_json_ld(42)),
        ):
            result = get_review_count("Some Film")

        self.assertIsInstance(result, int)
        self.assertEqual(result, 42)


if __name__ == "__main__":
    unittest.main()
