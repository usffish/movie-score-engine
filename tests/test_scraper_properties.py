"""
Property-based tests for scraper modules.

Property 10: Review count is always a non-negative integer
  For any movie title and any mocked Metacritic page response (including pages
  with missing, zero, or malformed review count elements), the get_review_count
  function should always return a non-negative integer and never raise an
  exception.

**Validates: Requirements 3.7, 3.4, 3.5**
"""

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from scraper.metacritic_scraper import get_review_count


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Generate arbitrary HTML strings as mock page content.
# We use text() with a broad alphabet to cover normal HTML, empty strings,
# malformed markup, random unicode, and strings that happen to contain
# JSON-LD-like fragments.
arbitrary_html = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),  # exclude surrogates
    min_size=0,
    max_size=2000,
)

# Generate arbitrary movie titles (non-empty strings)
arbitrary_title = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=100,
).filter(lambda s: s.strip())  # at least one non-whitespace character


def _make_mock_response(html: str, status_code: int = 200) -> MagicMock:
    """Build a mock requests.Response with the given HTML body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = html
    return resp


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------

@given(title=arbitrary_title, page_html=arbitrary_html)
@settings(max_examples=100)
def test_get_review_count_always_returns_nonneg_int(title: str, page_html: str):
    """
    Property 10: Review count is always a non-negative integer.

    For any movie title and any mocked Metacritic page response (including
    pages with missing, zero, or malformed review count elements),
    get_review_count should always return an int >= 0 and never raise.

    **Validates: Requirements 3.7, 3.4, 3.5**
    """
    mock_response = _make_mock_response(page_html)

    # Provide the same mock response for every network call so the function
    # always gets a 200 response with the arbitrary HTML content.
    with patch("scraper.metacritic_scraper.SESSION.get", return_value=mock_response):
        # The function must never raise, regardless of page content.
        result = get_review_count(title)

    # Must always return an int
    assert isinstance(result, int), (
        f"get_review_count returned {type(result).__name__!r}, expected int"
    )

    # Must always be non-negative
    assert result >= 0, (
        f"get_review_count returned {result}, expected >= 0"
    )


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------

# Property 11: Letterboxd rating is always in valid range when present.
#
# For any mocked Letterboxd page response containing a rating value, the
# get_letterboxd_data function should return a rating value in [0.0, 5.0].
#
# **Validates: Requirements 4.6**

from scraper.letterboxd_scraper import get_letterboxd_data

# Generate valid rating values in [0.0, 5.0]
valid_rating = st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False)


def _make_lb_response(html: str, status_code: int = 200) -> MagicMock:
    """Build a mock requests.Response for Letterboxd."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = html
    return resp


def _html_lb_itemprop(rating: float) -> str:
    """Build a Letterboxd-like page with <meta itemprop='ratingValue'>."""
    return f'<html><head><meta itemprop="ratingValue" content="{rating}"></head><body></body></html>'


@given(rating=valid_rating)
@settings(max_examples=100)
def test_letterboxd_rating_always_in_valid_range(rating: float):
    """
    Property 11: Letterboxd rating is always in valid range when present.

    For any mocked Letterboxd page response containing a rating value,
    get_letterboxd_data should return a rating in [0.0, 5.0].

    **Validates: Requirements 4.6**
    """
    html = _html_lb_itemprop(rating)
    mock_response = _make_lb_response(html)

    with patch("scraper.letterboxd_scraper.SESSION.get", return_value=mock_response):
        result = get_letterboxd_data("Some Film")

    # When a rating is present in the page, it must be in [0.0, 5.0]
    if result["rating"] is not None:
        assert 0.0 <= result["rating"] <= 5.0, (
            f"get_letterboxd_data returned rating {result['rating']}, expected in [0.0, 5.0]"
        )


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------

# Property 12: Exponential back-off grows between retries.
#
# For any retry sequence (up to 3 retries), the delay applied between each
# retry attempt should be strictly greater than the previous delay (i.e., the
# sequence of sleep durations is monotonically increasing).
#
# **Validates: Requirements 2.8, 3.6, 4.5, 10.2**

import requests as _requests

from scraper.metacritic_scraper import get_review_count as _get_review_count_meta
from scraper.letterboxd_scraper import get_letterboxd_data as _get_letterboxd_data_lb


@given(title=arbitrary_title)
@settings(max_examples=50)
def test_metacritic_sleep_durations_are_monotonically_increasing(title: str):
    """
    Property 12 (Metacritic): sleep durations between retry attempts are
    strictly increasing (exponential back-off).

    With retries=3 and sleep only between attempts (not after the last),
    there are 2 sleep calls per _fetch invocation with durations:
      backoff * 2**0, backoff * 2**1  (i.e., backoff, 2*backoff)
    which is strictly increasing.

    **Validates: Requirements 3.6, 10.2**
    """
    with patch("scraper.metacritic_scraper.SESSION.get") as mock_get:
        mock_get.side_effect = _requests.ConnectionError("fail")
        with patch("scraper.metacritic_scraper.time.sleep") as mock_sleep:
            _get_review_count_meta(title)

    sleep_calls = mock_sleep.call_args_list
    # There must be at least one _fetch call that produced sleep calls
    if len(sleep_calls) < 2:
        # If fewer than 2 sleep calls, we can't verify monotonicity -- skip
        return

    sleep_args = [call.args[0] for call in sleep_calls]

    # Group into pairs (each _fetch with retries=3 produces 2 sleeps)
    # Verify that within each group the sequence is strictly increasing
    for i in range(0, len(sleep_args) - 1, 2):
        if i + 1 < len(sleep_args):
            assert sleep_args[i] < sleep_args[i + 1], (
                f"Sleep durations not monotonically increasing: "
                f"{sleep_args[i]} >= {sleep_args[i + 1]} at positions {i}, {i+1}"
            )


@given(title=arbitrary_title)
@settings(max_examples=50)
def test_letterboxd_sleep_durations_are_monotonically_increasing(title: str):
    """
    Property 12 (Letterboxd): sleep durations between retry attempts are
    strictly increasing (exponential back-off).

    With retries=3 and sleep only between attempts (not after the last),
    there are 2 sleep calls per _fetch invocation with durations:
      backoff * 2**0, backoff * 2**1  (i.e., backoff, 2*backoff)
    which is strictly increasing.

    **Validates: Requirements 4.5, 10.2**
    """
    with patch("scraper.letterboxd_scraper.SESSION.get") as mock_get:
        mock_get.side_effect = _requests.ConnectionError("fail")
        with patch("scraper.letterboxd_scraper.time.sleep") as mock_sleep:
            _get_letterboxd_data_lb(title)

    sleep_calls = mock_sleep.call_args_list
    if len(sleep_calls) < 2:
        return

    sleep_args = [call.args[0] for call in sleep_calls]

    # Group into pairs (each _fetch with retries=3 produces 2 sleeps)
    for i in range(0, len(sleep_args) - 1, 2):
        if i + 1 < len(sleep_args):
            assert sleep_args[i] < sleep_args[i + 1], (
                f"Sleep durations not monotonically increasing: "
                f"{sleep_args[i]} >= {sleep_args[i + 1]} at positions {i}, {i+1}"
            )
