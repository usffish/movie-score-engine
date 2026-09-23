"""
Metacritic scraper using requests + BeautifulSoup.
Fetches the critic review count and, when available, the Metascore for a film.

Metacritic film pages follow the pattern:
  https://www.metacritic.com/movie/<slug>/

When a film has only 1–3 critic reviews, Metacritic does not display an
aggregate Metascore.  In that case this module fetches the individual review
scores from the critic-reviews sub-page and averages them to produce a
synthetic Metascore.
"""

import json
import logging
import re
import unicodedata
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scraper.http import retry_get, slugify as _slugify_base, years_differ

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.metacritic.com/",
}

try:
    from curl_cffi import requests as _curl_requests
except ImportError:  # pragma: no cover - optional dependency
    _curl_requests = None

# Metacritic sits behind Cloudflare, which fingerprints the TLS handshake.
# Older Python/OpenSSL builds (e.g. Python 3.9 + OpenSSL 1.1.1) get a 403
# "Just a moment..." challenge even with browser headers, so use curl_cffi's
# Chrome-impersonating session when available.
if _curl_requests is not None:
    SESSION = _curl_requests.Session(impersonate="chrome")
else:
    SESSION = requests.Session()
SESSION.headers.update(HEADERS)

_MOVIE_URL = "https://www.metacritic.com/movie/{slug}/"
_REVIEWS_URL = "https://www.metacritic.com/movie/{slug}/critic-reviews/"
_SEARCH_URL = "https://www.metacritic.com/search/{query}/?category=2"  # category 2 = movies

# Metacritic only shows an aggregate Metascore once a film has at least this
# many critic reviews.  Below this threshold we average the individual scores.
_MIN_REVIEWS_FOR_AGGREGATE = 4


def _slugify(text: str) -> str:
    """Metacritic-style slug: strips leading articles before hyphenating."""
    text = re.sub(r"^(the|a|an)\s+", "", text.lower().strip())
    return _slugify_base(text)


def _slugify_with_article(text: str) -> str:
    """Slug keeping leading articles (fallback candidate)."""
    return _slugify_base(text)


def _fetch(url: str, retries: int = 3, backoff: float = 2.5,
           rate_limiter=None, domain: str = "metacritic.com") -> Optional[BeautifulSoup]:
    """GET a URL and return a BeautifulSoup object, or None on failure/404."""
    resp = retry_get(
        SESSION, url, retries=retries, backoff=backoff,
        rate_limiter=rate_limiter, domain=domain,
        abort_on_404=True, label="Metacritic",
    )
    return BeautifulSoup(resp.text, "lxml") if resp is not None else None


def _extract_review_count(soup: BeautifulSoup) -> Optional[int]:
    """Extract the critic review count from a Metacritic movie page."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            agg = data.get("aggregateRating", {})
            count = agg.get("reviewCount") or agg.get("ratingCount")
            if count is not None:
                return int(count)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    count_selectors = [
        "span[class*='count'] a",
        "div[class*='summary'] span.count a",
        "span.based_on",
    ]
    for sel in count_selectors:
        tag = soup.select_one(sel)
        if tag:
            nums = re.findall(r"\d+", tag.get_text())
            if nums:
                return int(nums[0])

    return None


def _extract_aggregate_score(soup: BeautifulSoup) -> Optional[int]:
    """
    Extract the aggregate Metascore from a Metacritic movie page.

    Returns None when the score is not present (e.g. fewer than 4 reviews).
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            agg = data.get("aggregateRating", {})
            value = agg.get("ratingValue")
            if value is not None:
                score = round(float(value))
                return max(0, min(100, score))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    for tag in soup.find_all(string=re.compile(r"\bMetascore\b", re.IGNORECASE)):
        parent = tag.parent
        for _ in range(4):
            if parent is None:
                break
            nums = re.findall(r"\b(\d{1,3})\b", parent.get_text())
            for n in nums:
                val = int(n)
                if 0 <= val <= 100:
                    return val
            parent = parent.parent

    return None


def _extract_individual_scores(soup: BeautifulSoup) -> list:
    """
    Parse individual critic scores from a Metacritic critic-reviews page.

    Returns a list of ints (0–100).
    """
    scores = []
    pattern = re.compile(r"Metascore\s+(\d{1,3})\s+out\s+of\s+100", re.IGNORECASE)
    for text in soup.find_all(string=pattern):
        for match in pattern.finditer(text):
            val = int(match.group(1))
            if 0 <= val <= 100:
                scores.append(val)
    return scores


def _extract_release_year(soup: BeautifulSoup) -> Optional[int]:
    """Extract the release year from the page's JSON-LD datePublished (e.g. '2019-03-20')."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            published = data.get("datePublished") or data.get("dateCreated")
            if published:
                match = re.match(r"(\d{4})", str(published))
                if match:
                    return int(match.group(1))
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            continue
    return None


def _year_mismatch(soup: BeautifulSoup, year: Optional[int]) -> bool:
    """True when a year was requested and the page is clearly for a different film."""
    if not year:
        return False
    return years_differ(_extract_release_year(soup), year)


def _search_for_slug(title: str, rate_limiter=None) -> Optional[str]:
    """Search Metacritic and return the slug of the best matching movie."""
    query = re.sub(r"\s+", "%20", title.strip())
    url = _SEARCH_URL.format(query=query)
    soup = _fetch(url, rate_limiter=rate_limiter, domain="metacritic.com")
    if soup is None:
        return None

    title_lower = title.lower().strip()

    for link in soup.select("a[href^='/movie/']"):
        href = link["href"]
        parts = href.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "movie":
            slug = parts[1]
            link_text = link.get_text(strip=True).lower()
            if link_text == title_lower:
                return slug

    first = soup.select_one("a[href^='/movie/']")
    if first:
        href = first["href"]
        parts = href.strip("/").split("/")
        if len(parts) >= 2:
            return parts[1]

    return None


def get_metacritic_data(title: str, year: Optional[int] = None, resolver=None,
                        rate_limiter=None) -> dict:
    """
    Fetch critic review count and Metascore for a movie from Metacritic.

    When the film has 4+ reviews the aggregate Metascore is read directly from
    the movie page.  When it has 1–3 reviews Metacritic does not publish an
    aggregate, so this function fetches the critic-reviews sub-page, parses
    each individual score, and returns their rounded average.

    Args:
        title:    Movie title.
        year:     Optional release year.  Year-suffixed slugs (buddy-2026) are
                  tried first, and pages whose release year differs by more
                  than YEAR_TOLERANCE are skipped — the plain slug belongs to whichever
                  film claimed the title first (/movie/buddy/ is 2019's).
        resolver: Optional GeminiResolver instance.  When all local slug
                  candidates and the site search have failed, the resolver is
                  asked for the correct slug as a last resort.

    Returns:
        dict with keys:
            review_count (int):         >= 0; 0 when not found or on error.
            metascore    (int | None):  0–100; None when unavailable.
            year         (int | None):  release year of the matched page.
    """
    result: dict = {"review_count": 0, "metascore": None, "year": None}

    slug_no_article = _slugify(title)
    slug_with_article = _slugify_with_article(title)

    base_slugs = [slug_no_article]
    if slug_with_article != slug_no_article:
        base_slugs.append(slug_with_article)

    slugs = [f"{s}-{year}" for s in base_slugs] if year else []
    slugs += base_slugs

    soup = None
    matched_slug = None

    for slug in slugs:
        url = _MOVIE_URL.format(slug=slug)
        candidate = _fetch(url, rate_limiter=rate_limiter, domain="metacritic.com")
        if candidate is None:
            continue
        if _year_mismatch(candidate, year):
            logger.info(
                "Metacritic: %s is from %s, not %s — skipping",
                url, _extract_release_year(candidate), year,
            )
            continue
        soup = candidate
        matched_slug = slug
        break

    if soup is None:
        logger.info("Metacritic: direct slug failed for '%s', trying search", title)
        matched_slug = _search_for_slug(title)
        if matched_slug:
            url = _MOVIE_URL.format(slug=matched_slug)
            soup = _fetch(url, rate_limiter=rate_limiter, domain="metacritic.com")
            if soup is not None and _year_mismatch(soup, year):
                soup = None

    if soup is None and resolver is not None:
        logger.info("Metacritic: site search failed for '%s', asking Gemini", title)
        gemini_slug = resolver.resolve_metacritic_slug(title)
        if gemini_slug:
            url = _MOVIE_URL.format(slug=gemini_slug)
            soup = _fetch(url, rate_limiter=rate_limiter, domain="metacritic.com")
            if soup is not None:
                matched_slug = gemini_slug
                logger.info("Metacritic: Gemini resolved slug '%s' for '%s'", gemini_slug, title)

    if soup is None:
        logger.warning("Metacritic: could not find page for '%s'", title)
        return result

    return _extract_scores_from_soup(soup, matched_slug, rate_limiter, label=title)


def _extract_scores_from_soup(soup, slug: str, rate_limiter=None, label: str = "") -> dict:
    """
    Extract review_count and metascore from an already-fetched movie page.

    Handles both the aggregate path (4+ reviews) and the individual-score
    averaging path (1–3 reviews).  *label* is used only for log messages.
    """
    result: dict = {"review_count": 0, "metascore": None, "year": _extract_release_year(soup)}

    count = _extract_review_count(soup)
    if count is not None:
        result["review_count"] = count

    if result["review_count"] >= _MIN_REVIEWS_FOR_AGGREGATE:
        score = _extract_aggregate_score(soup)
        if score is not None:
            result["metascore"] = score
        elif label:
            logger.debug(
                "Metacritic: aggregate score not found on main page for '%s' (%d reviews)",
                label, result["review_count"],
            )
    elif result["review_count"] > 0:
        if label:
            logger.info(
                "Metacritic: %d review(s) for '%s' — averaging individual scores",
                result["review_count"], label,
            )
        reviews_url = _REVIEWS_URL.format(slug=slug)
        reviews_soup = _fetch(reviews_url, rate_limiter=rate_limiter, domain="metacritic.com")
        if reviews_soup is not None:
            scores = _extract_individual_scores(reviews_soup)
            if scores:
                avg = round(sum(scores) / len(scores))
                result["metascore"] = max(0, min(100, avg))
                if label:
                    logger.info(
                        "Metacritic: averaged %d individual score(s) → %d for '%s'",
                        len(scores), result["metascore"], label,
                    )
            elif label:
                logger.warning("Metacritic: could not parse individual scores for '%s'", label)

    return result


def get_metacritic_data_with_slug(slug: Optional[str], rate_limiter=None) -> dict:
    """
    Fetch Metacritic data using a pre-resolved slug.
    Returns dict with review_count and metascore.
    """
    if not slug:
        return {"review_count": 0, "metascore": None, "year": None}

    url = _MOVIE_URL.format(slug=slug)
    soup = _fetch(url, rate_limiter=rate_limiter)

    if soup is None:
        return {"review_count": 0, "metascore": None, "year": None}

    return _extract_scores_from_soup(soup, slug, rate_limiter)


def get_review_count(title: str, year: Optional[int] = None, resolver=None) -> int:
    """
    Backward-compatible wrapper around get_metacritic_data.

    Returns only the critic review count.  Prefer get_metacritic_data for new
    call sites so that the Metascore is also available.
    """
    return get_metacritic_data(title, year, resolver=resolver)["review_count"]
