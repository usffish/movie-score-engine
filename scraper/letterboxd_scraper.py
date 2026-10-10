"""
Letterboxd scraper using requests + BeautifulSoup.
Fetches the average community rating and number of ratings for a film.

Letterboxd film pages follow the pattern:
  https://letterboxd.com/film/<slug>/
where the slug is a URL-friendly version of the title.
"""

import json
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scraper.http import normalise_title, retry_get, slugify as _slugify, titles_match, years_differ

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# The site's own search pages (letterboxd.com/search/...) sit behind a
# Cloudflare challenge and answer 403; the API's search does not.
_SEARCH_URL = "https://api.letterboxd.com/api/v0/search"
_FILM_URL = "https://letterboxd.com/film/{slug}/"
# Redirects to the film's page; an unknown ID gets a 200 "not found" page.
_IMDB_URL = "https://letterboxd.com/imdb/{imdb_id}/"



def _fetch(url: str, retries: int = 3, backoff: float = 2.0,
           rate_limiter=None, domain: str = "letterboxd.com") -> Optional[BeautifulSoup]:
    """GET a URL and return a BeautifulSoup object, or None on failure/404."""
    resp = retry_get(
        SESSION, url, retries=retries, backoff=backoff,
        rate_limiter=rate_limiter, domain=domain,
        abort_on_404=True, label="Letterboxd",
    )
    return BeautifulSoup(resp.text, "lxml") if resp is not None else None


def _parse_rating_from_soup(soup: BeautifulSoup) -> Optional[float]:
    """Extract the average rating from a Letterboxd film page."""
    meta = soup.find("meta", itemprop="ratingValue")
    if meta and meta.get("content"):
        try:
            return max(0.0, min(5.0, float(meta["content"])))
        except ValueError:
            pass

    script = soup.find("script", type="application/ld+json")
    if script:
        try:
            data = json.loads(script.string or "")
            agg = data.get("aggregateRating", {})
            val = agg.get("ratingValue")
            if val is not None:
                return max(0.0, min(5.0, float(val)))
        except (json.JSONDecodeError, TypeError):
            pass

    twitter_meta = soup.find("meta", attrs={"name": "twitter:data2"})
    if twitter_meta and twitter_meta.get("content"):
        content = twitter_meta["content"]
        match = re.match(r"([\d.]+)\s+out\s+of", content, re.IGNORECASE)
        if match:
            try:
                return max(0.0, min(5.0, float(match.group(1))))
            except ValueError:
                pass

    for selector in ("span.average-rating", "span.display-rating"):
        tag = soup.select_one(selector)
        if tag:
            text = tag.get_text(strip=True)
            try:
                return max(0.0, min(5.0, float(text)))
            except ValueError:
                pass

    return None


def _parse_review_count_from_soup(soup: BeautifulSoup) -> Optional[int]:
    """Extract the number of ratings from a Letterboxd film page."""
    meta = soup.find("meta", itemprop="ratingCount")
    if meta and meta.get("content"):
        try:
            return int(meta["content"])
        except ValueError:
            pass

    script = soup.find("script", type="application/ld+json")
    if script:
        try:
            data = json.loads(script.string or "")
            agg = data.get("aggregateRating", {})
            count = agg.get("ratingCount")
            if count is not None:
                return int(count)
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def _parse_year_from_soup(soup: BeautifulSoup) -> Optional[int]:
    """Extract the release year from a Letterboxd film page's og:title, e.g. 'Parasite (1982)'."""
    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content"):
        match = re.search(r"\((\d{4})\)\s*$", meta["content"])
        if match:
            return int(match.group(1))
    return None


def _parse_title_from_soup(soup: BeautifulSoup) -> Optional[str]:
    """Film title from og:title with the trailing year removed: 'Parasite (2019)' -> 'Parasite'."""
    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content"):
        return re.sub(r"\s*\(\d{4}\)\s*$", "", meta["content"]).strip()
    return None


def _year_mismatch(soup: BeautifulSoup, year: Optional[int]) -> bool:
    """True when a year was requested and the page is clearly for a different film."""
    if not year:
        return False
    return years_differ(_parse_year_from_soup(soup), year)


def _canonical_film_url(soup: BeautifulSoup) -> Optional[str]:
    """The film page's own URL from og:url, or None when *soup* isn't a film page."""
    meta = soup.find("meta", property="og:url")
    url = meta.get("content") if meta else None
    if isinstance(url, str) and re.search(r"/film/[^/]+/?$", url):
        return url
    return None


def _search_queries(title: str) -> list:
    """
    Queries to try for *title*, most specific first: the title itself, then
    without bracketed notes, then without a subtitle.  The search returns
    nothing for "The Paradise (Telugu)" or "Adore Him: He is Here" but finds
    "The Paradise" and "Adore Him".
    """
    title = title.strip()
    plain = re.sub(r"\s*[(\[][^)\]]*[)\]]", "", title).strip()
    queries = []
    for query in (title, plain, plain.split(":")[0].strip()):
        if query and query not in queries:
            queries.append(query)
    return queries


def _search_films(query: str, rate_limiter=None) -> list:
    """Search Letterboxd for *query*; returns [(name, year, slug)] in ranked order."""
    resp = retry_get(
        SESSION, _SEARCH_URL,
        params={"input": query, "include": "FilmSearchItem", "perPage": 10},
        rate_limiter=rate_limiter, domain="letterboxd.com",
        abort_on_404=True, label="Letterboxd",
    )
    if resp is None:
        return []

    films = []
    try:
        for item in resp.json().get("items", []):
            film = item.get("film") or {}
            for link in film.get("links", []):
                match = re.search(r"letterboxd\.com/film/([^/]+)", link.get("url", ""))
                if link.get("type") == "letterboxd" and match:
                    films.append((film.get("name"), film.get("releaseYear"), match.group(1)))
                    break
    except (ValueError, TypeError, AttributeError):
        return []
    return films


def _search_for_slug(title: str, year: Optional[int] = None, rate_limiter=None) -> Optional[str]:
    """
    Search Letterboxd and return the slug of the best matching film.

    A result must name the same film (titles_match).  Failing that, one whose
    title contains ours or the reverse is accepted when a known year confirms
    it — a row titled "The Paradise (Telugu)" is Letterboxd's "The Paradise".
    """
    wanted = normalise_title(title)

    for query in _search_queries(title):
        films = _search_films(query, rate_limiter=rate_limiter)

        for name, film_year, slug in films:
            if titles_match(name, title) and not years_differ(film_year, year):
                return slug

        for name, film_year, slug in films:
            found = normalise_title(name or "")
            if not wanted or not found or not year or not film_year or years_differ(film_year, year):
                continue
            if f" {wanted} " in f" {found} " or f" {found} " in f" {wanted} ":
                return slug

    return None


def _candidate_slugs(title: str, year: Optional[int] = None) -> list:
    """
    Return an ordered list of slug candidates to try for a given title.

    Letterboxd commonly uses:
      - slug with year:        the-dark-knight-2008  (tried first when year is known)
      - plain slug:            the-dark-knight
      - slug with suffix -1:   the-dark-knight-1
      - slug with suffix -2:   the-dark-knight-2

    The year-suffixed slug goes first because the plain slug belongs to
    whichever film claimed the title first (e.g. /film/parasite/ is the 1982
    film; the 2019 one is /film/parasite-2019/).
    """
    base = _slugify(title)
    candidates = []
    if year:
        candidates.append(f"{base}-{year}")
    candidates.append(base)
    candidates.append(f"{base}-1")
    candidates.append(f"{base}-2")
    return candidates


def get_letterboxd_data(title: str, year: Optional[int] = None, resolver=None,
                        rate_limiter=None, imdb_id: Optional[str] = None) -> dict:
    """
    Fetch Letterboxd average rating and rating count for a movie.

    Args:
        title:    Movie title.
        year:     Optional release year — used to try a year-suffixed slug first.
        resolver: Optional GeminiResolver instance.  When all local slug
                  candidates and the site search have failed, the resolver is
                  asked for the correct slug as a last resort.
        imdb_id:  Optional IMDb ID of the film (e.g. from OMDb).  When the
                  slug candidates fail, Letterboxd is asked for the film
                  with that ID before falling back to search.

    Returns:
        dict with keys: rating (float|None), rating_count (int|None), url (str|None),
        year (int|None, release year of the matched page)
    """
    result = {"rating": None, "rating_count": None, "url": None, "year": None}

    for slug in _candidate_slugs(title, year):
        url = _FILM_URL.format(slug=slug)
        soup = _fetch(url, rate_limiter=rate_limiter, domain="letterboxd.com")
        if soup is not None:
            if _year_mismatch(soup, year):
                logger.info(
                    "Letterboxd: %s is from %s, not %s — skipping",
                    url, _parse_year_from_soup(soup), year,
                )
                continue
            result["url"] = url
            result["rating"] = _parse_rating_from_soup(soup)
            result["rating_count"] = _parse_review_count_from_soup(soup)
            result["year"] = _parse_year_from_soup(soup)
            return result

    if imdb_id:
        logger.info("Letterboxd: direct slugs failed for '%s', trying IMDb ID %s", title, imdb_id)
        soup = _fetch(_IMDB_URL.format(imdb_id=imdb_id), rate_limiter=rate_limiter,
                      domain="letterboxd.com")
        url = _canonical_film_url(soup) if soup is not None else None
        if url is not None and not _year_mismatch(soup, year):
            result["url"] = url
            result["rating"] = _parse_rating_from_soup(soup)
            result["rating_count"] = _parse_review_count_from_soup(soup)
            result["year"] = _parse_year_from_soup(soup)
            return result

    logger.info("Letterboxd: direct slugs failed for '%s', trying search", title)
    slug = _search_for_slug(title, year=year, rate_limiter=rate_limiter)
    if slug is not None:
        url = _FILM_URL.format(slug=slug)
        soup = _fetch(url, rate_limiter=rate_limiter, domain="letterboxd.com")
        if soup is not None and not _year_mismatch(soup, year):
            result["url"] = url
            result["rating"] = _parse_rating_from_soup(soup)
            result["rating_count"] = _parse_review_count_from_soup(soup)
            result["year"] = _parse_year_from_soup(soup)
            return result

    if resolver is not None:
        logger.info("Letterboxd: site search failed for '%s', asking Gemini", title)
        gemini_slug = resolver.resolve_letterboxd_slug(title, year)
        if gemini_slug:
            url = _FILM_URL.format(slug=gemini_slug)
            soup = _fetch(url, rate_limiter=rate_limiter, domain="letterboxd.com")
            if soup is not None and (
                not titles_match(_parse_title_from_soup(soup), title) or _year_mismatch(soup, year)
            ):
                logger.warning(
                    "Letterboxd: rejected Gemini slug '%s' for '%s' — it's '%s' (%s)",
                    gemini_slug, title, _parse_title_from_soup(soup), _parse_year_from_soup(soup),
                )
                resolver.mark_rejected(title, year, "letterboxd_slug", gemini_slug)
                soup = None
            if soup is not None:
                logger.info("Letterboxd: Gemini resolved slug '%s' for '%s'", gemini_slug, title)
                result["url"] = url
                result["rating"] = _parse_rating_from_soup(soup)
                result["rating_count"] = _parse_review_count_from_soup(soup)
                result["year"] = _parse_year_from_soup(soup)
                return result

    logger.warning("Letterboxd: could not find '%s'", title)
    return result


def get_letterboxd_data_with_slug(slug: Optional[str], rate_limiter=None) -> dict:
    """
    Fetch Letterboxd data using a pre-resolved slug.
    Returns dict with rating, rating_count, url, year, and title (the page's
    film title, so callers can check it's the right film).
    """
    empty = {"rating": None, "rating_count": None, "url": None, "year": None, "title": None}
    if not slug:
        return empty

    url = _FILM_URL.format(slug=slug)
    soup = _fetch(url, rate_limiter=rate_limiter)

    if soup is None:
        return empty

    return {
        "rating": _parse_rating_from_soup(soup),
        "rating_count": _parse_review_count_from_soup(soup),
        "url": url,
        "year": _parse_year_from_soup(soup),
        "title": _parse_title_from_soup(soup),
    }
