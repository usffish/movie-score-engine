"""
Property-based tests for scraper/omdb_client.py.

Property 2: OMDb response parsing round-trip
  For any valid Metascore integer string in "0"–"100" and any valid imdbRating
  decimal string in "0.0"–"10.0", the OMDb client should parse them to the
  correct int and float values respectively, and the parsed values should
  satisfy 0 ≤ metascore ≤ 100 and 0.0 ≤ imdb_rating ≤ 10.0.

**Validates: Requirements 2.3, 2.4**
"""

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from scraper.omdb_client import get_omdb_data


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Valid Metascore strings: integers 0–100
metascore_strings = st.integers(min_value=0, max_value=100).map(str)

# Valid imdbRating strings: one decimal place, 0.0–10.0
# Build as (integer part 0–10) + "." + (decimal digit 0–9), then clamp to 10.0
imdb_rating_strings = st.builds(
    lambda whole, frac: f"{whole}.{frac}",
    whole=st.integers(min_value=0, max_value=10),
    frac=st.integers(min_value=0, max_value=9),
).filter(
    # Exclude values > 10.0 (e.g. "10.5" is invalid per OMDb spec)
    lambda s: float(s) <= 10.0
)


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------

@given(metascore_str=metascore_strings, imdb_rating_str=imdb_rating_strings)
@settings(max_examples=100)
def test_omdb_parsing_round_trip(metascore_str: str, imdb_rating_str: str):
    """
    Property 2: OMDb response parsing round-trip.

    For any valid Metascore string in "0"–"100" and any valid imdbRating
    string in "0.0"–"10.0", the parsed values satisfy:
      - 0 ≤ metascore ≤ 100  (int)
      - 0.0 ≤ imdb_rating ≤ 10.0  (float)

    **Validates: Requirements 2.3, 2.4**
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Response": "True",
        "Metascore": metascore_str,
        "imdbRating": imdb_rating_str,
        "imdbID": "tt0000001",
    }
    mock_resp.url = "http://www.omdbapi.com/"

    with patch("scraper.omdb_client.SESSION.get", return_value=mock_resp):
        result = get_omdb_data("Any Movie", "testkey")

    metascore = result["metascore"]
    imdb_rating = result["imdb_rating"]

    # metascore must be an int in [0, 100]
    assert isinstance(metascore, int), f"metascore should be int, got {type(metascore)}"
    assert 0 <= metascore <= 100, f"metascore {metascore} out of range [0, 100]"

    # imdb_rating must be a float in [0.0, 10.0]
    assert isinstance(imdb_rating, float), (
        f"imdb_rating should be float, got {type(imdb_rating)}"
    )
    assert 0.0 <= imdb_rating <= 10.0, (
        f"imdb_rating {imdb_rating} out of range [0.0, 10.0]"
    )

    # Round-trip: parsed value must equal the original string value
    assert metascore == int(metascore_str), (
        f"metascore {metascore} != int({metascore_str!r})"
    )
    assert abs(imdb_rating - float(imdb_rating_str)) < 1e-9, (
        f"imdb_rating {imdb_rating} != float({imdb_rating_str!r})"
    )
