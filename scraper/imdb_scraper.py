"""
IMDB scraper using the cinemagoer library.
Searches for a movie by title and returns its rating and review count.
"""

import logging
from typing import Optional
from imdb import Cinemagoer

logger = logging.getLogger(__name__)

_ia = None


def _get_ia() -> Cinemagoer:
    """Lazy-init the Cinemagoer instance (one per process)."""
    global _ia
    if _ia is None:
        _ia = Cinemagoer()
    return _ia


def get_imdb_data(title: str, year: Optional[int] = None) -> dict:
    """
    Fetch IMDB rating and vote count for a movie.

    Args:
        title: Movie title to search for.
        year:  Optional release year to narrow the match.

    Returns:
        dict with keys: rating (float|None), votes (int|None), imdb_id (str|None)
    """
    result = {"rating": None, "votes": None, "imdb_id": None}
    ia = _get_ia()

    try:
        results = ia.search_movie(title, results=5)
        if not results:
            logger.warning("IMDB: no results for '%s'", title)
            return result

        # Pick the best match: prefer exact title + year match, else first result
        movie = _best_match(results, title, year)
        if movie is None:
            logger.warning("IMDB: no suitable match for '%s'", title)
            return result

        ia.update(movie, info=["main"])

        result["imdb_id"] = movie.movieID
        result["rating"] = movie.get("rating")
        result["votes"] = movie.get("votes")

    except Exception as exc:
        logger.error("IMDB error for '%s': %s", title, exc)

    return result


def _best_match(results, title: str, year: Optional[int]):
    """Return the best matching movie from a list of search results."""
    title_lower = title.lower().strip()

    # First pass: exact title + year
    if year:
        for m in results:
            if (
                m.get("title", "").lower().strip() == title_lower
                and m.get("year") == year
            ):
                return m

    # Second pass: exact title only
    for m in results:
        if m.get("title", "").lower().strip() == title_lower:
            return m

    # Fallback: first result
    return results[0]
