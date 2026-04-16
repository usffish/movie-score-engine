"""
Metacritic scraper using requests + BeautifulSoup.
Fetches only the critic review count for a film.

Metacritic film pages follow the pattern:
  https://www.metacritic.com/movie/<slug>/
"""

import json
import logging
import re
import time
import unicodedata
from typing import Optional

import requests
from bs4 import BeautifulSoup

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

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

_MOVIE_URL = "https://www.metacritic.com/movie/{slug}/"
_SEARCH_URL = "https://www.metacritic.com/search/{query}/?category=2"  # category 2 = movies


def _slugify(text: str) -> str:
    """Convert a title to a Metacritic-style URL slug (strips leading articles)."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    # Remove articles at the start (Metacritic sometimes drops "the", "a", "an")
    text = re.sub(r"^(the|a|an)\s+", "", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text


def _slugify_with_article(text: str) -> str:
    """Slugify keeping leading articles."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text


def _fetch(url: str, retries: int = 3, backoff: float = 2.5) -> Optional[BeautifulSoup]:
    """GET a URL and return a BeautifulSoup object, with retry logic and exponential back-off."""
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, timeout=15)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "lxml")
            if resp.status_code == 404:
                return None
            logger.warning("Metacritic: HTTP %s for %s", resp.status_code, url)
        except requests.RequestException as exc:
            logger.warning("Metacritic: request error (%s) for %s", exc, url)
        # Only sleep between attempts, not after the last one
        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))
    return None


def _extract_review_count(soup: BeautifulSoup) -> Optional[int]:
    """Extract the critic review count from a Metacritic movie page."""
    # Try JSON-LD structured data first
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

    # Fallback: HTML selectors for review count
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


def _search_for_slug(title: str) -> Optional[str]:
    """Search Metacritic and return the slug of the best matching movie."""
    query = re.sub(r"\s+", "%20", title.strip())
    url = _SEARCH_URL.format(query=query)
    soup = _fetch(url)
    if soup is None:
        return None

    title_lower = title.lower().strip()

    # Search result links look like /movie/<slug>/
    for link in soup.select("a[href^='/movie/']"):
        href = link["href"]
        parts = href.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "movie":
            slug = parts[1]
            link_text = link.get_text(strip=True).lower()
            if link_text == title_lower:
                return slug

    # Fallback: first movie link
    first = soup.select_one("a[href^='/movie/']")
    if first:
        href = first["href"]
        parts = href.strip("/").split("/")
        if len(parts) >= 2:
            return parts[1]

    return None


def get_review_count(title: str, year: Optional[int] = None) -> int:
    """
    Fetch the critic review count for a movie from Metacritic.

    Tries the direct slug URL (with and without leading article), then falls
    back to a search query. Returns 0 on any failure.

    Args:
        title: Movie title.
        year:  Optional release year (unused in URL construction but kept for
               API compatibility).

    Returns:
        int: Critic review count (>= 0). Returns 0 when not found or on error.
    """
    # Build candidate slugs to try
    slug_no_article = _slugify(title)
    slug_with_article = _slugify_with_article(title)

    slugs = [slug_no_article]
    if slug_with_article != slug_no_article:
        slugs.append(slug_with_article)

    for slug in slugs:
        url = _MOVIE_URL.format(slug=slug)
        soup = _fetch(url)
        if soup is None:
            continue
        count = _extract_review_count(soup)
        if count is not None:
            return count

    # Fall back to search
    logger.info("Metacritic: direct slug failed for '%s', trying search", title)
    slug = _search_for_slug(title)
    if slug:
        url = _MOVIE_URL.format(slug=slug)
        soup = _fetch(url)
        if soup is not None:
            count = _extract_review_count(soup)
            if count is not None:
                return count

    logger.warning("Metacritic: could not find review count for '%s'", title)
    return 0
