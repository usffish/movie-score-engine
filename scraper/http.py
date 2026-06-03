"""
scraper/http.py
===============
Shared HTTP fetch utility: retry loop, adaptive rate limiting, slug helpers.
"""

import logging
import re
import time
import unicodedata
from collections import defaultdict
from threading import Lock
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def slugify(text: str) -> str:
    """Convert a title to a hyphenated ASCII slug (shared base for all scrapers)."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text


class RateLimiter:
    """
    Per-domain rate limiter with adaptive backoff.

    Tracks request timestamps per domain and enforces minimum delay between
    requests. Automatically increases delay when rate limit errors (429/503)
    are detected.
    """

    def __init__(self, base_delay: float = 1.0, max_delay: float = 30.0):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.domain_delays = defaultdict(lambda: base_delay)
        self.last_request_time = defaultdict(float)
        self.lock = Lock()

    def get_delay_for_domain(self, domain: str) -> float:
        with self.lock:
            return self.domain_delays[domain]

    def increase_delay(self, domain: str, factor: float = 2.0):
        with self.lock:
            current = self.domain_delays[domain]
            new_delay = min(current * factor, self.max_delay)
            self.domain_delays[domain] = new_delay
            logger.warning("Rate limit hit for %s, increasing delay to %.1fs", domain, new_delay)

    def decrease_delay(self, domain: str, factor: float = 0.9, min_delay: float = None):
        with self.lock:
            current = self.domain_delays[domain]
            new_delay = max(current * factor, min_delay or self.base_delay)
            self.domain_delays[domain] = new_delay

    def wait_if_needed(self, domain: str):
        with self.lock:
            now = time.time()
            sleep_time = max(0.0, self.domain_delays[domain] - (now - self.last_request_time.get(domain, 0)))
        if sleep_time:
            time.sleep(sleep_time)
        with self.lock:
            self.last_request_time[domain] = time.time()


def retry_get(
    session: requests.Session,
    url: str,
    params: Optional[dict] = None,
    retries: int = 3,
    backoff: float = 2.0,
    rate_limiter=None,
    domain: str = "",
    abort_on_404: bool = False,
    label: str = "",
) -> Optional[requests.Response]:
    """
    GET a URL with retry and optional rate limiting.

    Returns the Response on success (HTTP 200), None after all retries exhausted.
    When abort_on_404=True, returns None immediately on 404 without retrying.
    On 429/503, increases the rate limit delay and retries with doubled backoff.
    """
    pfx = f"{label}: " if label else ""

    for attempt in range(retries):
        try:
            if rate_limiter:
                rate_limiter.wait_if_needed(domain)

            resp = session.get(url, params=params, timeout=15)

            if resp.status_code in (429, 503):
                logger.warning(
                    "%sRate limit HTTP %s for %s (attempt %d/%d)",
                    pfx, resp.status_code, url, attempt + 1, retries,
                )
                if rate_limiter:
                    rate_limiter.increase_delay(domain)
                time.sleep(backoff * (2 ** attempt) * 2)
                continue

            if resp.status_code == 200:
                if rate_limiter and attempt == 0:
                    rate_limiter.decrease_delay(domain)
                return resp

            if abort_on_404 and resp.status_code == 404:
                return None

            logger.warning(
                "%sHTTP %s for %s (attempt %d/%d)",
                pfx, resp.status_code, url, attempt + 1, retries,
            )

        except requests.RequestException as exc:
            logger.warning(
                "%sRequest error (%s) for %s (attempt %d/%d)",
                pfx, exc, url, attempt + 1, retries,
            )

        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))

    return None
