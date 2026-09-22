"""
manual.py
=========
Interactive prompts for filling in missing scores manually.
"""

import logging
from typing import Optional

from scoring import RawScores

logger = logging.getLogger(__name__)


class ManualEntryInterrupted(Exception):
    """
    Raised when the user Ctrl-Cs out of a manual entry prompt.

    Carries the fields entered before the interrupt so they are not lost.
    """

    def __init__(self, partial: Optional[RawScores] = None):
        super().__init__("Manual entry interrupted")
        self.partial = partial


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


def _progress_label(position: Optional[int], total: Optional[int]) -> str:
    """Render a ' [3/12 · 9 left]' suffix, or '' when progress is unknown."""
    if position is None or total is None:
        return ""
    return f" [{position}/{total} · {total - position} left]"


def prompt_missing_scores(
    raw: RawScores,
    position: Optional[int] = None,
    total: Optional[int] = None,
) -> RawScores:
    """
    Interactively prompt the user to fill in any None/zero fields on *raw*.

    Returns a new RawScores with user-supplied values merged in.
    """
    print(f"\n  ── Manual entry for: {raw.title} ──{_progress_label(position, total)}")
    print("  (Press Enter to skip a field and leave it unchanged, Ctrl-C to stop and save)\n")

    metascore = raw.metascore
    imdb_rating = raw.imdb_rating
    review_count = raw.review_count
    letterboxd_rating = raw.letterboxd_rating

    def snapshot() -> RawScores:
        return RawScores(
            title=raw.title,
            metascore=metascore,
            imdb_rating=imdb_rating,
            review_count=review_count,
            letterboxd_rating=letterboxd_rating,
        )

    try:
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
    except KeyboardInterrupt:
        raise ManualEntryInterrupted(snapshot()) from None

    return snapshot()


def prompt_failed_movie(
    title: str,
    position: Optional[int] = None,
    total: Optional[int] = None,
) -> Optional[RawScores]:
    """
    Interactively prompt the user to enter all scores for a movie that
    failed entirely during fetch.

    Returns a RawScores if the user provides at least one value, or None
    if the user skips all fields.
    """
    print(f"\n  ── Manual entry for failed movie: {title} ──{_progress_label(position, total)}")
    print("  (Press Enter to skip a field, Ctrl-C to stop and save)\n")

    metascore = None
    imdb_rating = None
    review_count = 0
    letterboxd_rating = None

    def snapshot() -> Optional[RawScores]:
        if all(v is None for v in (metascore, imdb_rating, letterboxd_rating)) and review_count == 0:
            return None
        return RawScores(
            title=title,
            metascore=metascore,
            imdb_rating=imdb_rating,
            review_count=review_count,
            letterboxd_rating=letterboxd_rating,
        )

    try:
        metascore = _prompt_int_in_range("  Metascore (0-100): ", 0, 100, "Metascore")
        imdb_rating = _prompt_float_in_range("  IMDB rating (0.0-10.0): ", 0.0, 10.0, "IMDB rating")
        rc = _prompt_int_in_range("  Critic review count (0+): ", 0, 100_000, "review count")
        review_count = rc if rc is not None else 0
        letterboxd_rating = _prompt_float_in_range(
            "  Letterboxd rating (0.0-5.0): ", 0.0, 5.0, "Letterboxd rating"
        )
    except KeyboardInterrupt:
        raise ManualEntryInterrupted(snapshot()) from None

    result = snapshot()
    if result is None:
        logger.info("Skipped manual entry for '%s'", title)
    return result


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

    Ctrl-C stops the prompting but keeps every entry made so far, so the
    caller can still write them out.

    Returns:
        (raw_scores, failed, manual_unchanged)
        where manual_unchanged is a set of titles whose manual entries were
        identical to the existing workbook values.
    """
    manual_unchanged: set = set()

    if not manual:
        return raw_scores, failed, manual_unchanged

    existing = existing or {}

    def _has_missing(raw: RawScores) -> bool:
        return (
            raw.metascore is None
            or raw.imdb_rating is None
            or raw.review_count == 0
            or raw.letterboxd_rating is None
        )

    total = sum(1 for raw in raw_scores if _has_missing(raw)) + len(failed)
    position = 0
    entered = 0
    interrupted = False

    updated_raw = []
    for raw in raw_scores:
        if _has_missing(raw) and not interrupted:
            position += 1
            try:
                filled = prompt_missing_scores(raw, position, total)
            except ManualEntryInterrupted as exc:
                interrupted = True
                filled = exc.partial
            if filled is not None:
                prev = existing.get(raw.title)
                if prev is not None and _manual_matches_existing(filled, prev):
                    manual_unchanged.add(raw.title)
                if filled != raw:
                    entered += 1
                raw = filled
        updated_raw.append(raw)

    still_failed = []
    for title in failed:
        if interrupted:
            still_failed.append(title)
            continue
        position += 1
        try:
            result = prompt_failed_movie(title, position, total)
        except ManualEntryInterrupted as exc:
            interrupted = True
            result = exc.partial
        if result is not None:
            prev = existing.get(title)
            if prev is not None and _manual_matches_existing(result, prev):
                manual_unchanged.add(title)
            updated_raw.append(result)
            entered += 1
        else:
            still_failed.append(title)

    if interrupted:
        plural = "entry" if entered == 1 else "entries"
        print(f"\n  Manual entry stopped at {position} of {total} — "
              f"keeping the {entered} {plural} already made.\n")
        logger.warning(
            "Manual entry interrupted at %d of %d movie(s); %d %s kept",
            position, total, entered, plural,
        )

    return updated_raw, still_failed, manual_unchanged
