"""
manual.py
=========
Interactive prompts for filling in missing scores manually.
"""

import logging
from typing import Optional

from scoring import RawScores

logger = logging.getLogger(__name__)


def _prompt_value(prompt: str, parser, label: str):
    """
    Prompt the user for a value, parse it with *parser*, and return the result.

    Returns None if the user presses Enter without typing anything (skip).
    Loops until a valid value is entered or the user skips.
    """
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return None
        try:
            return parser(raw)
        except (ValueError, TypeError):
            print(f"  Invalid {label}. Press Enter to skip, or try again.")


def _prompt_int_in_range(prompt: str, lo: int, hi: int, label: str) -> Optional[int]:
    """Prompt for an integer in [lo, hi], returning None on empty input."""
    def parse(s):
        v = int(s)
        if not (lo <= v <= hi):
            raise ValueError(f"{v} not in [{lo}, {hi}]")
        return v
    return _prompt_value(prompt, parse, label)


def _prompt_float_in_range(prompt: str, lo: float, hi: float, label: str) -> Optional[float]:
    """Prompt for a float in [lo, hi], returning None on empty input."""
    def parse(s):
        v = float(s)
        if not (lo <= v <= hi):
            raise ValueError(f"{v} not in [{lo}, {hi}]")
        return v
    return _prompt_value(prompt, parse, label)


def prompt_missing_scores(raw: RawScores) -> RawScores:
    """
    Interactively prompt the user to fill in any None/zero fields on *raw*.

    Returns a new RawScores with user-supplied values merged in.
    """
    print(f"\n  ── Manual entry for: {raw.title} ──")
    print("  (Press Enter to skip a field and leave it unchanged)\n")

    metascore = raw.metascore
    imdb_rating = raw.imdb_rating
    review_count = raw.review_count
    letterboxd_rating = raw.letterboxd_rating

    if metascore is None:
        metascore = _prompt_int_in_range("  Metascore (0-100): ", 0, 100, "Metascore")

    if imdb_rating is None:
        imdb_rating = _prompt_float_in_range("  IMDB rating (0.0-10.0): ", 0.0, 10.0, "IMDB rating")

    if review_count == 0:
        rc = _prompt_int_in_range("  Critic review count (0+): ", 0, 100_000, "review count")
        if rc is not None:
            review_count = rc

    if letterboxd_rating is None:
        letterboxd_rating = _prompt_float_in_range(
            "  Letterboxd rating (0.0-5.0): ", 0.0, 5.0, "Letterboxd rating"
        )

    return RawScores(
        title=raw.title,
        metascore=metascore,
        imdb_rating=imdb_rating,
        review_count=review_count,
        letterboxd_rating=letterboxd_rating,
    )


def prompt_failed_movie(title: str) -> Optional[RawScores]:
    """
    Interactively prompt the user to enter all scores for a movie that
    failed entirely during fetch.

    Returns a RawScores if the user provides at least one value, or None
    if the user skips all fields.
    """
    print(f"\n  ── Manual entry for failed movie: {title} ──")
    print("  (Press Enter to skip a field)\n")

    metascore = _prompt_int_in_range("  Metascore (0-100): ", 0, 100, "Metascore")
    imdb_rating = _prompt_float_in_range("  IMDB rating (0.0-10.0): ", 0.0, 10.0, "IMDB rating")
    rc = _prompt_int_in_range("  Critic review count (0+): ", 0, 100_000, "review count")
    review_count = rc if rc is not None else 0
    letterboxd_rating = _prompt_float_in_range(
        "  Letterboxd rating (0.0-5.0): ", 0.0, 5.0, "Letterboxd rating"
    )

    if all(v is None for v in (metascore, imdb_rating, letterboxd_rating)) and review_count == 0:
        logger.info("Skipped manual entry for '%s'", title)
        return None

    return RawScores(
        title=title,
        metascore=metascore,
        imdb_rating=imdb_rating,
        review_count=review_count,
        letterboxd_rating=letterboxd_rating,
    )


def _manual_matches_existing(new: RawScores, prev: RawScores) -> bool:
    """
    Return True if every score field in *new* that was manually entered
    matches the corresponding field already stored in *prev*.
    """
    _EPS = 1e-9

    def _eq(a, b) -> bool:
        if a is None or b is None:
            return a == b
        if isinstance(a, float) or isinstance(b, float):
            return abs(float(a) - float(b)) < _EPS
        return a == b

    if new.metascore is not None and not _eq(new.metascore, prev.metascore):
        return False
    if new.imdb_rating is not None and not _eq(new.imdb_rating, prev.imdb_rating):
        return False
    if new.review_count and not _eq(new.review_count, prev.review_count):
        return False
    if new.letterboxd_rating is not None and not _eq(new.letterboxd_rating, prev.letterboxd_rating):
        return False
    return True


def apply_manual_entry(
    raw_scores: list[RawScores],
    failed: list[str],
    manual: bool,
    existing: Optional[dict[str, RawScores]] = None,
) -> tuple[list[RawScores], list[str], set[str]]:
    """
    After Pass 1, optionally prompt the user for missing values.

    Returns:
        (raw_scores, failed, manual_unchanged)
        where manual_unchanged is a set of titles whose manual entries were
        identical to the existing workbook values.
    """
    manual_unchanged: set = set()

    if not manual:
        return raw_scores, failed, manual_unchanged

    existing = existing or {}

    updated_raw = []
    for raw in raw_scores:
        has_missing = (
            raw.metascore is None
            or raw.imdb_rating is None
            or raw.review_count == 0
            or raw.letterboxd_rating is None
        )
        if has_missing:
            filled = prompt_missing_scores(raw)
            prev = existing.get(raw.title)
            if prev is not None and _manual_matches_existing(filled, prev):
                manual_unchanged.add(raw.title)
            raw = filled
        updated_raw.append(raw)

    still_failed = []
    for title in failed:
        result = prompt_failed_movie(title)
        if result is not None:
            prev = existing.get(title)
            if prev is not None and _manual_matches_existing(result, prev):
                manual_unchanged.add(title)
            updated_raw.append(result)
        else:
            still_failed.append(title)

    return updated_raw, still_failed, manual_unchanged
