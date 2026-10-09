"""
manual.py
=========
Interactive prompts for filling in missing scores manually.
"""

import dataclasses
import logging
from typing import Optional

from scoring import RawScores, format_source_years

logger = logging.getLogger(__name__)


class ManualEntryInterrupted(Exception):
    """
    Raised when the user Ctrl-Cs out of a manual entry prompt.

    Carries the fields entered before the interrupt so they are not lost.
    """

    def __init__(self, partial: Optional[RawScores] = None):
        super().__init__("Manual entry interrupted")
        self.partial = partial


class TitleCorrection(Exception):
    """Raised when the user asks to fix the title of the movie being prompted."""

    def __init__(self, new_title: str):
        super().__init__(f"Title corrected to {new_title!r}")
        self.new_title = new_title


# Returned by a prompt when the user typed a command instead of a value.
BACK = object()
RETITLE = object()


def _prompt_value(prompt: str, parser, label: str, commands: Optional[dict] = None):
    """
    Prompt the user for a value, parse it with *parser*, and return the result.

    Returns None if the user presses Enter without typing anything (skip).
    Loops until a valid value is entered or the user skips.
    *commands* maps a typed word (e.g. "b") to a sentinel returned as-is.
    End of input (no terminal attached, or Ctrl-Z/Ctrl-D) is treated like
    Ctrl-C, so callers stop prompting and keep what was entered.
    """
    while True:
        try:
            # Windows PowerShell prefixes piped stdin with a BOM.
            raw = input(prompt).lstrip("\ufeff").strip()
        except EOFError:
            raise KeyboardInterrupt from None
        if raw == "":
            return None
        if commands and raw.lower() in commands:
            return commands[raw.lower()]
        try:
            return parser(raw)
        except (ValueError, TypeError):
            print(f"  Invalid {label}. Press Enter to skip, or try again.")


def _prompt_int_in_range(prompt: str, lo: int, hi: int, label: str,
                         commands: Optional[dict] = None) -> Optional[int]:
    """Prompt for an integer in [lo, hi], returning None on empty input."""
    def parse(s):
        v = int(s)
        if not (lo <= v <= hi):
            raise ValueError(f"{v} not in [{lo}, {hi}]")
        return v
    return _prompt_value(prompt, parse, label, commands)


def _prompt_float_in_range(prompt: str, lo: float, hi: float, label: str,
                           commands: Optional[dict] = None) -> Optional[float]:
    """Prompt for a float in [lo, hi], returning None on empty input."""
    def parse(s):
        v = float(s)
        if not (lo <= v <= hi):
            raise ValueError(f"{v} not in [{lo}, {hi}]")
        return v
    return _prompt_value(prompt, parse, label, commands)


def _progress_label(position: Optional[int], total: Optional[int]) -> str:
    """Render a ' [3/12 · 9 left]' suffix, or '' when progress is unknown."""
    if position is None or total is None:
        return ""
    return f" [{position}/{total} · {total - position} left]"


def _hint(allow_retitle: bool) -> str:
    """The one-line key reminder printed above a movie's prompts."""
    keys = ["Enter skips a field", "b goes back"]
    if allow_retitle:
        keys.append("t fixes the title")
    keys.append("Ctrl-C stops and saves")
    return f"  ({' · '.join(keys)})\n"


# Score fields in the order they're asked: (RawScores field, label, prompt).
_SCORE_PROMPTS = (
    ("metascore", "Metascore",
     lambda cmds: _prompt_int_in_range("  Metascore (0-100): ", 0, 100, "Metascore", cmds)),
    ("imdb_rating", "IMDB",
     lambda cmds: _prompt_float_in_range("  IMDB rating (0.0-10.0): ", 0.0, 10.0, "IMDB rating", cmds)),
    ("review_count", "Reviews",
     lambda cmds: _prompt_int_in_range("  Critic review count (0+): ", 0, 100_000, "review count", cmds)),
    ("letterboxd_rating", "Letterboxd",
     lambda cmds: _prompt_float_in_range("  Letterboxd rating (0.0-5.0): ", 0.0, 5.0, "Letterboxd rating", cmds)),
)


def _prompt_fields(start: RawScores, allow_retitle: bool = False) -> RawScores:
    """
    Ask for each score that is blank on *start* and return a copy with the
    answers merged in.  The critic review count is only asked for alongside a
    Metascore typed in now — without a Metacritic page there's no count to know.

    Typing "b" returns to the previous field; "t" (when *allow_retitle*) asks
    for a corrected title and raises TitleCorrection.  Ctrl-C raises
    ManualEntryInterrupted carrying the fields entered so far.
    """
    commands = {"b": BACK}
    if allow_retitle:
        commands["t"] = RETITLE

    entered: dict = {}
    asked: list = []   # indexes of the fields answered so far, for "b"

    def wanted(field: str) -> bool:
        if field == "review_count":
            return start.review_count == 0 and entered.get("metascore") is not None
        return getattr(start, field) is None

    def snapshot() -> RawScores:
        return dataclasses.replace(
            start, **{f: v for f, v in entered.items() if v is not None}
        )

    i = 0
    try:
        while i < len(_SCORE_PROMPTS):
            field, _, ask = _SCORE_PROMPTS[i]
            if not wanted(field):
                i += 1
                continue
            value = ask(commands)
            if value is BACK:
                if not asked:
                    print("  Already at the first field.")
                    continue
                i = asked.pop()
                entered.pop(_SCORE_PROMPTS[i][0], None)
                continue
            if value is RETITLE:
                new_title = _prompt_value("  Correct title (Enter to cancel): ", str, "title")
                if new_title and new_title != start.title:
                    raise TitleCorrection(new_title)
                continue
            entered[field] = value
            asked.append(i)
            i += 1
    except KeyboardInterrupt:
        raise ManualEntryInterrupted(snapshot()) from None

    return snapshot()


def prompt_missing_scores(
    raw: RawScores,
    position: Optional[int] = None,
    total: Optional[int] = None,
    allow_retitle: bool = False,
) -> RawScores:
    """
    Interactively prompt the user to fill in the blank fields on *raw*.

    *raw* should already carry the workbook's existing values for anything the
    scrapers didn't return, so fields filled on an earlier run aren't asked
    again.  The critic review count is only asked for alongside a Metascore
    typed in now — without a Metacritic page there's no count to know.

    Returns a new RawScores with user-supplied values merged in.
    """
    print(f"\n  ── Manual entry for: {raw.title} ──{_progress_label(position, total)}")
    print(_hint(allow_retitle))
    return _prompt_fields(raw, allow_retitle)


def has_blank_scores(raw: Optional[RawScores]) -> bool:
    """True when a score the user could type in is still blank (Reviews alone doesn't count)."""
    return raw is None or (
        raw.metascore is None or raw.imdb_rating is None or raw.letterboxd_rating is None
    )


def prompt_failed_movie(
    title: str,
    position: Optional[int] = None,
    total: Optional[int] = None,
    existing: Optional[RawScores] = None,
    allow_retitle: bool = False,
) -> Optional[RawScores]:
    """
    Interactively prompt the user for the scores of a movie that failed
    entirely during fetch.

    Fields already filled in *existing* (the workbook row) aren't asked again,
    and the critic review count is only asked for alongside a Metascore typed
    in now.

    Returns a RawScores (existing values plus new entries) if the user enters
    at least one value, or None if nothing new was entered.
    """
    print(f"\n  ── Manual entry for failed movie: {title} ──{_progress_label(position, total)}")
    print(_hint(allow_retitle))

    start = dataclasses.replace(existing or RawScores(title, None, None, 0, None), title=title)

    try:
        result = _prompt_fields(start, allow_retitle)
    except ManualEntryInterrupted as exc:
        raise ManualEntryInterrupted(None if exc.partial == start else exc.partial) from None

    if result == start:
        logger.info("Skipped manual entry for '%s'", title)
        return None
    return result


def prompt_unknown_years(raw_scores: list[RawScores]) -> tuple[dict, bool]:
    """
    Ask for the release year of every movie whose year is unknown — the
    sources disagreed about which film it is, or none reported a year.

    Shows what each source matched as a hint.  Blank input skips a movie.

    Returns:
        (years, interrupted) where years maps title -> entered year, and
        interrupted is True when the user Ctrl-C'd (entries so far are kept).
    """
    unknown = [r for r in raw_scores if r.year is None]
    years: dict = {}
    if not unknown:
        return years, False

    total = len(unknown)
    print(f"\n  ── Release year unknown for {total} movie(s) ──")
    print("  The sources may have matched different films. Enter the year you mean,")
    print("  and the movie is re-fetched with it. (Enter to skip, Ctrl-C to stop)\n")

    for position, raw in enumerate(unknown, start=1):
        print(f"  {raw.title}{_progress_label(position, total)}")
        print(f"    found: {format_source_years(raw.source_years)}")
        try:
            year = _prompt_int_in_range("    Release year: ", 1870, 2100, "year")
        except KeyboardInterrupt:
            print(f"\n  Year entry stopped — keeping {len(years)} year(s) entered.\n")
            return years, True
        if year is not None:
            years[raw.title] = year

    return years, False


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


@dataclasses.dataclass
class _Entry:
    """One movie in the manual-entry queue."""
    title: str
    base: Optional[RawScores]      # scores before any typing; None when the fetch failed
    result: Optional[RawScores]    # scores to write; None when there is nothing to write
    position: Optional[int] = None # place in the prompt queue; None when not asked


def _typed_fields(entry: _Entry, existing: dict) -> list:
    """[(label, value)] for the scores typed in for *entry* this run."""
    if entry.result is None:
        return []
    before = entry.base or existing.get(entry.title) or RawScores(entry.title, None, None, 0, None)
    return [
        (label, getattr(entry.result, field))
        for field, label, _ in _SCORE_PROMPTS
        if getattr(entry.result, field) != getattr(before, field)
    ]


def _ask_entry(entry: _Entry, total: int, existing: dict, retitle) -> bool:
    """
    Prompt for one movie, following any title corrections, and store the
    outcome on *entry*.  Returns True when the user interrupted.
    """
    while True:
        try:
            if entry.base is not None:
                entry.result = prompt_missing_scores(
                    entry.base, entry.position, total, allow_retitle=retitle is not None
                )
            else:
                entry.result = prompt_failed_movie(
                    entry.title, entry.position, total,
                    existing=existing.get(entry.title), allow_retitle=retitle is not None,
                )
            return False
        except ManualEntryInterrupted as exc:
            entry.result = exc.partial
            return True
        except TitleCorrection as exc:
            try:
                refetched = retitle(entry.title, exc.new_title)
            except ValueError as err:
                print(f"  {err}")
                continue
            except KeyboardInterrupt:
                entry.result = entry.base
                return True
            entry.title = exc.new_title
            entry.base = entry.result = refetched
            if refetched is None:
                print(f"  Still couldn't fetch '{entry.title}'.")
            elif not has_blank_scores(refetched):
                print(f"  Fetched '{entry.title}' — nothing left to enter.")
                return False


def _review_entries(asked: list, total: int, existing: dict, retitle) -> bool:
    """
    Show every score typed this run and let the user redo a movie or discard
    the lot before anything is saved.  Returns True when the user interrupted
    a redo (what was typed is kept, as with any other interrupt).
    """
    while True:
        typed = [(e, _typed_fields(e, existing)) for e in asked]
        typed = [(e, fields) for e, fields in typed if fields]
        if not typed:
            return False

        print(f"\n  ── Review: typed scores for {len(typed)} movie(s) ──")
        for n, (entry, fields) in enumerate(typed, start=1):
            summary = " · ".join(f"{label} {value}" for label, value in fields)
            print(f"  {n:>3}. {entry.title}: {summary}")
        try:
            choice = input(
                "\n  Enter to save, a number to redo that movie, d to discard typed scores: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if choice == "":
            return False
        if choice == "d":
            for entry, _ in typed:
                entry.result = entry.base
            print("  Typed scores discarded.")
            return False
        if choice.isdigit() and 1 <= int(choice) <= len(typed):
            entry = typed[int(choice) - 1][0]
            if _ask_entry(entry, total, existing, retitle):
                return True
            continue
        print(f"  Enter a number from 1 to {len(typed)}, d, or just Enter.")


def apply_manual_entry(
    raw_scores: list[RawScores],
    failed: list[str],
    manual: bool,
    existing: Optional[dict[str, RawScores]] = None,
    retitle=None,
) -> tuple[list[RawScores], list[str], set[str]]:
    """
    After Pass 1, optionally prompt the user for missing values, then show
    what was typed for review before it is saved.

    Ctrl-C stops the prompting but keeps every entry made so far, so the
    caller can still write them out.

    *retitle*, when given, enables fixing a movie's title from the prompt.
    It is called as retitle(old_title, new_title), must rename the movie
    wherever the caller tracks it (including *existing*) and re-fetch it,
    and returns the re-fetched RawScores or None when the fetch failed.  It
    raises ValueError with a message when the new title can't be used.

    Returns:
        (raw_scores, failed, manual_unchanged)
        where manual_unchanged is a set of titles whose manual entries were
        identical to the existing workbook values.
    """
    manual_unchanged: set = set()

    if not manual:
        return raw_scores, failed, manual_unchanged

    existing = existing if existing is not None else {}

    # raw_scores should already include the workbook's existing values, so
    # only fields that are blank everywhere are asked about.  A failed movie
    # whose row is already complete isn't asked about either.
    entries = [_Entry(raw.title, raw, raw) for raw in raw_scores]
    entries += [_Entry(title, None, None) for title in failed]
    to_ask = [
        e for e in entries
        if has_blank_scores(e.base if e.base is not None else existing.get(e.title))
    ]
    total = len(to_ask)
    asked = []
    interrupted = False

    for position, entry in enumerate(to_ask, start=1):
        entry.position = position
        asked.append(entry)
        if _ask_entry(entry, total, existing, retitle):
            interrupted = True
            break

    if interrupted:
        entered = sum(1 for e in asked if _typed_fields(e, existing))
        plural = "entry" if entered == 1 else "entries"
        print(f"\n  Manual entry stopped at {len(asked)} of {total} — "
              f"keeping the {entered} {plural} already made.\n")
        logger.warning(
            "Manual entry interrupted at %d of %d movie(s); %d %s kept",
            len(asked), total, entered, plural,
        )
    else:
        _review_entries(asked, total, existing, retitle)

    for entry in asked:
        prev = existing.get(entry.title)
        if entry.result is not None and prev is not None and _manual_matches_existing(entry.result, prev):
            manual_unchanged.add(entry.title)

    updated_raw = [e.result for e in entries if e.result is not None]
    still_failed = [e.title for e in entries if e.result is None]
    return updated_raw, still_failed, manual_unchanged
