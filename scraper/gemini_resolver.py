"""
gemini_resolver.py
==================
Uses the Gemini API to resolve the correct URL slug or identifier for a movie
on Metacritic, Letterboxd, and OMDb/IMDb when the local slug-guessing and
search fallbacks have already failed.

The resolver is intentionally narrow: it only asks Gemini for a slug string,
never for scores.  All score data still comes from the original sources, and
callers verify every answer (title + year) before using it.

Gemini answers with Google Search grounding, so it looks identifiers up
instead of recalling them.  Without grounding every model tested invented
IMDb IDs (Without Blood -> three different wrong IDs); with it all three
returned the right one.

Answers — and answers the caller rejected — are cached in a local JSON file
so repeat runs don't re-ask the same questions.

Usage
-----
    from scraper.gemini_resolver import GeminiResolver

    resolver = GeminiResolver(api_key="YOUR_GEMINI_KEY", cache_path=".gemini_cache.json")

    ids = resolver.resolve_all_ids("Without Blood", year=2024, want=("imdb_id",))
    slug = resolver.resolve_letterboxd_slug("Parasite", year=2019)

Environment variable
--------------------
    GEMINI_API_KEY — used when no api_key is passed to GeminiResolver().
"""

import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Iterable, Optional

from scraper.http import normalise_title

logger = logging.getLogger(__name__)

# Models in order of preference (strongest first).  When one hits its daily
# limit, is rate limited, or isn't available to this key, the next is tried.
# Format: (model_name, rpm, rpd)
_GEMINI_MODELS = [
    ("gemini-3-flash-preview",        1000,  10_000),  # Tier 1: 1K RPM, 10K RPD
    ("gemini-3.1-flash-lite-preview", 4000, 150_000),  # Tier 1: 4K RPM, 150K RPD
    ("gemini-2.5-flash-lite",         4000, 999_999),  # Tier 1: 4K RPM, unlimited RPD
]

# Identifier keys, one per source.
ID_KEYS = ("metacritic_slug", "letterboxd_slug", "imdb_id")

# Cached answers expire so films that had no page yet get re-checked later.
_CACHE_TTL_SECONDS = 30 * 86400

# Per-request timeout.  Grounded answers from gemini-3-flash-preview took
# 20–60 s in testing (lite models 1–4 s); a timeout falls back to the next
# model for that prompt.
_REQUEST_TIMEOUT_MS = 90_000

# Lazy import so the module can be imported even when google-genai is not
# installed — callers that never instantiate GeminiResolver won't break.
_genai = None


def _import_genai():
    global _genai
    if _genai is not None:
        return _genai
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
        _genai = (genai, types)
        return _genai
    except ImportError as exc:
        raise ImportError(
            "google-genai is required for Gemini slug resolution. "
            "Install it with: pip install google-genai"
        ) from exc


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_ID_DESCRIPTIONS = {
    "metacritic_slug": (
        "the Metacritic slug — the <slug> in https://www.metacritic.com/movie/<slug>/ "
        '(e.g. "dark-knight", "buddy-2026")'
    ),
    "letterboxd_slug": (
        "the Letterboxd slug — the <slug> in https://letterboxd.com/film/<slug>/ "
        '(e.g. "the-dark-knight", "parasite-2019")'
    ),
    "imdb_id": 'the IMDb ID — "tt" followed by 7 or 8 digits (e.g. "tt0468569")',
}

_PROMPT = """\
Use Google Search to look up the exact identifiers for the movie "{title}"{year_clause}.

I need:
{wanted}

Only give identifiers you have confirmed on the actual site for this specific
film. Several films can share a title — make sure the one you give is {which}.
If you cannot confirm one, use null. Never guess.

Reply with ONLY a JSON object with exactly these keys, and no other text:
{{{keys}}}
"""


def _build_prompt(title: str, year: Optional[int], want: Iterable[str]) -> str:
    want = list(want)
    year_clause = f" released in {year}" if year else ""
    which = f"the {year} film" if year else "the most notable film with that exact title"
    wanted = "\n".join(f"- {k}: {_ID_DESCRIPTIONS[k]}" for k in want)
    keys = ", ".join(f'"{k}": ...' for k in want)
    return _PROMPT.format(title=title, year_clause=year_clause, wanted=wanted,
                          which=which, keys=keys)


def _parse_json_reply(text: Optional[str]) -> Optional[dict]:
    """Pull the JSON object out of a reply, tolerating ```json fences or stray prose."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Answer cache
# ---------------------------------------------------------------------------

class _AnswerCache:
    """
    JSON file of {"<normalised title>|<year>": {"ts": epoch, "answers": {...},
    "rejected": {key: [values]}}}.  Missing or corrupt files start empty;
    write failures are logged and ignored (the cache is only an optimisation).
    """

    def __init__(self, path: Optional[Path]):
        self._path = Path(path) if path else None
        self._data: dict = {}
        if self._path and self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("GeminiResolver: ignoring unreadable cache %s (%s)", self._path, exc)
                self._data = {}

    @staticmethod
    def key(title: str, year: Optional[int]) -> str:
        return f"{normalise_title(title)}|{year or ''}"

    def get(self, title: str, year: Optional[int]) -> dict:
        entry = self._data.get(self.key(title, year))
        if not entry or time.time() - entry.get("ts", 0) > _CACHE_TTL_SECONDS:
            return {}
        return entry

    def store_answers(self, title: str, year: Optional[int], answers: dict) -> None:
        entry = self._data.setdefault(self.key(title, year), {"answers": {}, "rejected": {}})
        if time.time() - entry.get("ts", 0) > _CACHE_TTL_SECONDS:
            entry.update(answers={}, rejected={})
        entry["answers"].update(answers)
        entry["ts"] = time.time()
        self._save()

    def mark_rejected(self, title: str, year: Optional[int], key: str, value: str) -> None:
        entry = self._data.setdefault(self.key(title, year),
                                      {"answers": {}, "rejected": {}, "ts": time.time()})
        rejected = entry.setdefault("rejected", {}).setdefault(key, [])
        if value not in rejected:
            rejected.append(value)
        self._save()

    def _save(self) -> None:
        if not self._path:
            return
        try:
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("GeminiResolver: could not write cache %s (%s)", self._path, exc)


# ---------------------------------------------------------------------------
# Resolver class
# ---------------------------------------------------------------------------

class GeminiResolver:
    """
    Resolves movie slugs/identifiers using Gemini with Google Search grounding.

    Instantiate once per process and reuse across all scraper calls.
    The client is created lazily on first use.

    Includes built-in rate limiting and automatic model cycling:
    - Uses the strongest model first
    - Moves to the next model on daily limit, rate limit, or unavailability
    """

    def __init__(self, api_key: Optional[str] = None, cache_path=None, grounding: bool = True):
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self._api_key:
            raise ValueError(
                "Gemini API key is required. Pass api_key= or set GEMINI_API_KEY."
            )

        self._grounding = grounding
        self._cache = _AnswerCache(cache_path)

        # Model cycling state
        self._model_index = 0  # Start with best model
        self._models = _GEMINI_MODELS
        self._client = None  # lazy init

        # Per-model rate limiting: list of (minute_requests, day_requests) deques
        self._minute_requests = [deque() for _ in self._models]
        self._day_requests = [deque() for _ in self._models]

        # Warning throttling
        self._last_rpm_warning = 0
        self._last_rpd_warning = 0

    @property
    def _current_model_name(self) -> str:
        return self._models[self._model_index][0]

    @property
    def _current_rpm(self) -> int:
        return self._models[self._model_index][1]

    @property
    def _current_rpd(self) -> int:
        return self._models[self._model_index][2]

    def _get_client(self):
        if self._client is None:
            genai, types = _import_genai()
            # Without a timeout a stalled request blocks the whole run.
            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
            )
        return self._client

    def _config(self):
        _, types = _import_genai()
        kwargs = {
            # We call generate_content once per question; no function calling.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if self._grounding:
            kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        return types.GenerateContentConfig(**kwargs)

    def _switch_to_next_model(self, reason: str) -> bool:
        """
        Switch to the next available model.
        Returns True if switched, False if no more models available.
        """
        if self._model_index < len(self._models) - 1:
            self._model_index += 1
            logger.warning(
                "GeminiResolver: switching to model %s (%s on %s)",
                self._current_model_name, reason, self._models[self._model_index - 1][0],
            )
            return True
        return False

    def _wait_for_rate_limit(self) -> None:
        """
        Wait if necessary to respect RPM and RPD limits for current model.
        Uses a sliding window approach to track requests.
        """
        idx = self._model_index
        now = time.time()

        # Clean up old timestamps (older than 1 minute)
        while self._minute_requests[idx] and self._minute_requests[idx][0] < now - 60:
            self._minute_requests[idx].popleft()

        # Clean up old timestamps (older than 1 day)
        while self._day_requests[idx] and self._day_requests[idx][0] < now - 86400:
            self._day_requests[idx].popleft()

        # Check RPM limit
        if len(self._minute_requests[idx]) >= self._current_rpm:
            oldest = self._minute_requests[idx][0]
            wait_time = 60 - (now - oldest) + 0.1
            if wait_time > 0:
                if now - self._last_rpm_warning > 60:
                    logger.warning(
                        "GeminiResolver: RPM limit reached (%d/min) on %s, waiting %.1fs",
                        self._current_rpm, self._current_model_name, wait_time
                    )
                    self._last_rpm_warning = now
                time.sleep(wait_time)
                now = time.time()
                while self._minute_requests[idx] and self._minute_requests[idx][0] < now - 60:
                    self._minute_requests[idx].popleft()

        # Check RPD limit - if hit, try switching to next model
        if len(self._day_requests[idx]) >= self._current_rpd:
            if self._switch_to_next_model("daily limit reached"):
                # New model has different limits, recurse to check its limits
                self._wait_for_rate_limit()
                return
            else:
                # All models exhausted, wait for the first model's day to reset
                oldest = self._day_requests[0][0]
                wait_time = 86400 - (now - oldest) + 1
                if now - self._last_rpd_warning > 3600:
                    logger.warning(
                        "GeminiResolver: ALL models at RPD limit, waiting %.0fs for reset",
                        wait_time
                    )
                    self._last_rpd_warning = now
                time.sleep(wait_time)
                # Reset to best model after waiting
                self._model_index = 0
                return

        # Record this request
        self._minute_requests[idx].append(now)
        self._day_requests[idx].append(now)

    def _ask(self, prompt: str) -> Optional[str]:
        """
        Send a prompt to Gemini and return the stripped response text, or None.

        Rate limits, unavailable models, and models without search grounding
        move to the next model for the rest of the run.  A timeout or an
        empty reply falls back to the next model for this prompt only.
        """
        idx = self._model_index
        while idx < len(self._models):
            if idx == self._model_index:
                self._wait_for_rate_limit()
                idx = self._model_index  # may have moved on a daily limit
            model_name = self._models[idx][0]
            try:
                response = self._get_client().models.generate_content(
                    model=model_name, contents=prompt, config=self._config(),
                )
                text = (response.text or "").strip()
                logger.debug("GeminiResolver: %s returned %r", model_name, text)
                if text:
                    return text
                finish = response.candidates[0].finish_reason if response.candidates else None
                logger.warning("GeminiResolver: empty reply from %s (finish reason %s)",
                               model_name, finish)
            except Exception as exc:
                error_str = str(exc).lower()
                if "429" in error_str or "rate limit" in error_str or "quota" in error_str:
                    reason = "rate limited"
                elif "404" in error_str or "not_found" in error_str or "no longer available" in error_str:
                    reason = "model unavailable"
                elif self._grounding and ("google_search" in error_str or "tool" in error_str):
                    reason = "search grounding unsupported"
                elif ("timed out" in error_str or "timeout" in error_str
                      or "504" in error_str or "deadline" in error_str):
                    logger.warning("GeminiResolver: %s timed out", model_name)
                    reason = None
                else:
                    logger.warning("GeminiResolver: API error on %s — %s", model_name, exc)
                    return None
                if reason and idx == self._model_index:
                    if not self._switch_to_next_model(reason):
                        logger.error("GeminiResolver: no model left to try (%s)", reason)
                        return None
                    idx = self._model_index
                    continue
            idx += 1
        return None

    # ------------------------------------------------------------------
    # Public resolution methods
    # ------------------------------------------------------------------

    def resolve_all_ids(self, title: str, year: Optional[int] = None,
                        want: Iterable[str] = ID_KEYS) -> dict:
        """
        Ask Gemini for the identifiers in *want* (any of ID_KEYS) in one request.

        *year* is included in the prompt so Gemini picks the right film among
        same-titled ones.  Cached answers are reused; an answer the caller
        rejected (see mark_rejected) comes back as None without re-asking.

        Returns a dict with every key in ID_KEYS; keys not in *want* are None.
        """
        want = [k for k in ID_KEYS if k in set(want)]
        result = {k: None for k in ID_KEYS}
        if not want:
            return result

        cached = self._cache.get(title, year)
        answers = cached.get("answers", {})
        rejected = cached.get("rejected", {})
        missing = [k for k in want if k not in answers]

        if missing:
            logger.info("GeminiResolver: resolving %s for '%s'%s",
                        ", ".join(missing), title, f" ({year})" if year else "")
            reply = self._ask(_build_prompt(title, year, missing))
            data = _parse_json_reply(reply)
            if reply is not None and data is None:
                logger.warning("GeminiResolver: reply was not valid JSON: %r", reply)
            if data is not None:
                fresh = {k: _validate_id(k, data.get(k)) for k in missing}
                self._cache.store_answers(title, year, fresh)
                answers = {**answers, **fresh}
        else:
            logger.info("GeminiResolver: using cached answer for '%s'%s",
                        title, f" ({year})" if year else "")

        for k in want:
            value = answers.get(k)
            if value is not None and value not in rejected.get(k, []):
                result[k] = value
        return result

    def mark_rejected(self, title: str, year: Optional[int], key: str, value: str) -> None:
        """Record that *value* for *key* was checked and is the wrong film, so it isn't reused."""
        self._cache.mark_rejected(title, year, key, value)

    def resolve_metacritic_slug(self, title: str, year: Optional[int] = None) -> Optional[str]:
        """Return the Metacritic slug for *title* (e.g. "dark-knight") or None."""
        return self.resolve_all_ids(title, year, want=("metacritic_slug",))["metacritic_slug"]

    def resolve_letterboxd_slug(self, title: str, year: Optional[int] = None) -> Optional[str]:
        """Return the Letterboxd slug for *title* (e.g. "the-dark-knight") or None."""
        return self.resolve_all_ids(title, year, want=("letterboxd_slug",))["letterboxd_slug"]

    def resolve_imdb_id(self, title: str, year: Optional[int] = None) -> Optional[str]:
        """Return the IMDb ID for *title* (e.g. "tt0110912") or None."""
        return self.resolve_all_ids(title, year, want=("imdb_id",))["imdb_id"]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_id(key: str, value) -> Optional[str]:
    if key == "imdb_id":
        return _validate_imdb_id(value)
    return _validate_slug(value)


def _validate_slug(value: Optional[str]) -> Optional[str]:
    """
    Return *value* if it looks like a valid URL slug, else None.

    A valid slug contains only lowercase letters, digits, and hyphens,
    is between 1 and 120 characters, and does not start or end with a hyphen.
    Rejects multi-word responses (spaces) and anything that looks like a URL.
    """
    if not value or not isinstance(value, str):
        return None
    # Strip surrounding whitespace and quotes the model might add
    value = value.strip().strip('"\'')
    if value.lower() in ("null", "none", "unknown"):
        return None
    # Reject if it contains spaces (model gave a sentence instead of a slug)
    if " " in value:
        logger.warning("GeminiResolver: slug response looks like prose, discarding: %r", value)
        return None
    # Reject if it looks like a full URL
    if value.startswith("http"):
        logger.warning("GeminiResolver: slug response is a URL, discarding: %r", value)
        return None
    # Must match slug pattern: starts and ends with alnum, hyphens only in middle
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        logger.warning("GeminiResolver: slug response failed validation, discarding: %r", value)
        return None
    # Check length
    if len(value) > 120:
        logger.warning("GeminiResolver: slug too long (%d chars), discarding: %r", len(value), value)
        return None
    return value


def _validate_imdb_id(value: Optional[str]) -> Optional[str]:
    """
    Return *value* if it looks like a valid IMDb ID (tt + 7-8 digits), else None.
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip().strip('"\'')
    if value.lower() in ("null", "none", "unknown"):
        return None
    if re.fullmatch(r"tt\d{7,8}", value):
        return value
    logger.warning("GeminiResolver: IMDb ID response failed validation, discarding: %r", value)
    return None
