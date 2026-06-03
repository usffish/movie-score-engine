"""
OMDb API client using requests.
Fetches Metascore and IMDB rating for a film from the OMDb JSON API.

OMDb API endpoint:
  http://www.omdbapi.com/?t={title}&apikey={key}
  http://www.omdbapi.com/?t={title}&y={year}&apikey={key}
"""

import logging
from typing import Optional

import requests

from scraper.http import retry_get

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
    """
    params: dict = {"t": title, "apikey": api_key}
    if year is not None:
        params["y"] = year

    data = _fetch(_OMDB_URL, params, rate_limiter=rate_limiter, domain="omdbapi.com")

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
                    return {
                        "metascore": _parse_metascore(id_data.get("Metascore")),
                        "imdb_rating": _parse_imdb_rating(id_data.get("imdbRating")),
                        "imdb_id": id_data.get("imdbID") or imdb_id,
                    }

        return dict(_FALLBACK)

    return {
        "metascore": _parse_metascore(data.get("Metascore")),
        "imdb_rating": _parse_imdb_rating(data.get("imdbRating")),
        "imdb_id": data.get("imdbID") or None,
    }


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

    return {
        "metascore": _parse_metascore(data.get("Metascore")),
        "imdb_rating": _parse_imdb_rating(data.get("imdbRating")),
        "imdb_id": data.get("imdbID") or imdb_id,
    }
