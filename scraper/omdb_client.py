"""
OMDb API client using requests.
Fetches Metascore and IMDB rating for a film from the OMDb JSON API.

OMDb API endpoint:
  http://www.omdbapi.com/?t={title}&apikey={key}
  http://www.omdbapi.com/?t={title}&y={year}&apikey={key}
"""

import logging
import re
from typing import Optional

import requests

from scraper.http import retry_get, years_differ

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

_OMDB_URL = "http://www.omdbapi.com/"

_FALLBACK = {
    "metascore": None,
    "imdb_rating": None,
    "imdb_id": None,
    "year": None,
}


def _fetch(url: str, params: dict, retries: int = 3, backoff: float = 2.0,
           rate_limiter=None, domain: str = "omdbapi.com") -> Optional[dict]:
    """GET the OMDb API and return the parsed JSON dict, or None on failure."""
    resp = retry_get(
        SESSION, url, params=params, retries=retries, backoff=backoff,
        rate_limiter=rate_limiter, domain=domain, label="OMDb",
    )
    return resp.json() if resp is not None else None


def _parse_metascore(value: Optional[str]) -> Optional[int]:
    """Parse a Metascore string to int; return None when value is N/A or missing."""
    if not value or value == "N/A":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _parse_imdb_rating(value: Optional[str]) -> Optional[float]:
    """Parse an imdbRating string to float; return None when value is N/A or missing."""
    if not value or value == "N/A":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_year(value: Optional[str]) -> Optional[int]:
    """Parse OMDb's Year field ('2019', or '2019–2021' for series) to its first year."""
    if not value:
        return None
    match = re.match(r"(\d{4})", str(value))
    return int(match.group(1)) if match else None


def _to_result(data: dict, imdb_id_fallback: Optional[str] = None) -> dict:
    return {
        "metascore": _parse_metascore(data.get("Metascore")),
        "imdb_rating": _parse_imdb_rating(data.get("imdbRating")),
        "imdb_id": data.get("imdbID") or imdb_id_fallback,
        "year": _parse_year(data.get("Year")),
    }


def get_omdb_data(title: str, api_key: str, year: Optional[int] = None, resolver=None,
                  rate_limiter=None) -> dict:
    """
    Fetch Metascore and IMDB rating for a movie from the OMDb API.

    Args:
        title:    Movie title.
        api_key:  OMDb API key.
        year:     Optional release year to improve match accuracy.
        resolver: Optional GeminiResolver instance.  When OMDb cannot find the
                  movie by title, the resolver is asked for the IMDb ID and the
                  lookup is retried using that ID directly.

    Returns:
        dict with keys:
            metascore  (int|None):    0–100; None when N/A or not found
            imdb_rating (float|None): 0.0–10.0; None when N/A or not found
            imdb_id     (str|None):   IMDb ID (e.g. "tt0118749"); None when not found
            year        (int|None):   release year of the matched title
    """
    params: dict = {"t": title, "apikey": api_key}
    if year is not None:
        params["y"] = year

    data = _fetch(_OMDB_URL, params, rate_limiter=rate_limiter, domain="omdbapi.com")

    # OMDb only matches a film's first release year, so a theatrical-release
    # year finds nothing for a film that premiered earlier (Without Blood:
    # 2026 release, OMDb year 2024).  Retry without the year and accept the
    # result if it's close enough to be the same film.
    if data is not None and data.get("Response") == "False" and year is not None:
        retry = _fetch(_OMDB_URL, {"t": title, "apikey": api_key},
                       rate_limiter=rate_limiter, domain="omdbapi.com")
        if retry is not None and retry.get("Response") != "False":
            found_year = _parse_year(retry.get("Year"))
            if years_differ(found_year, year):
                logger.info(
                    "OMDb: '%s' without a year is from %s, not %s — ignoring",
                    title, found_year, year,
                )
            else:
                data = retry

    if data is None:
        logger.warning("OMDb: all retries exhausted for '%s', returning fallbacks", title)
        return dict(_FALLBACK)

    if data.get("Response") == "False":
        logger.warning("OMDb: movie not found for '%s': %s", title, data.get("Error", ""))

        if resolver is not None:
            logger.info("OMDb: asking Gemini for IMDb ID for '%s'", title)
            imdb_id = resolver.resolve_imdb_id(title)
            if imdb_id:
                id_data = _fetch(
                    _OMDB_URL, {"i": imdb_id, "apikey": api_key},
                    rate_limiter=rate_limiter, domain="omdbapi.com",
                )
                if id_data and id_data.get("Response") != "False":
                    logger.info("OMDb: Gemini resolved IMDb ID '%s' for '%s'", imdb_id, title)
                    return _to_result(id_data, imdb_id)

        return dict(_FALLBACK)

    return _to_result(data)


def get_omdb_data_with_id(api_key: str, imdb_id: Optional[str], rate_limiter=None) -> dict:
    """
    Fetch OMDb data using a pre-resolved IMDb ID.
    Returns dict with metascore, imdb_rating, imdb_id.
    """
    if not imdb_id:
        return dict(_FALLBACK)

    data = _fetch(_OMDB_URL, {"i": imdb_id, "apikey": api_key},
                  rate_limiter=rate_limiter, domain="omdbapi.com")

    if data is None or data.get("Response") == "False":
        return dict(_FALLBACK)

    return _to_result(data, imdb_id)
