#!/usr/bin/env python3
"""
update_scores.py
================
Reads Movies.xlsx, fetches the latest Metacritic / Letterboxd / IMDB scores
for every movie, recalculates the composite score, and writes the results to
Movies_updated.xlsx (the original file is never overwritten).

Usage
-----
    python update_scores.py                        # update all movies
    python update_scores.py --limit 10             # only 10 random movies (testing)
    python update_scores.py --movie "Boogie Nights" # single movie
    python update_scores.py --input my_list.xlsx   # custom input file
    python update_scores.py --delay 1.5            # seconds between requests
    python update_scores.py --api-key YOUR_KEY     # OMDb API key
    python update_scores.py --smart-update         # skip recently-stable movies
    python update_scores.py --manual               # prompt for missing values
    python update_scores.py --random               # process movies in random order

Output columns added / updated
-------------------------------
    Metacritic      - Metascore (0-100)
    st.Metacritic   - normalised 0-1
    Reviews         - number of critic reviews
    Letterboxd      - average rating (0-5)
    st.Letterboxd   - normalised 0-1
    IMDB            - IMDB rating (0-10)
    st.IMDB         - normalised 0-1
    TRUE            - composite score (weighted average of the three normalised scores)
    LastUpdated     - ISO date of last successful fetch (YYYY-MM-DD)
    StableWeeks     - consecutive weeks the composite score has been within ±0.05
"""

import argparse
import dataclasses
import logging
import os
import random
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from tqdm import tqdm

from excel import (
    MANUAL_FIELDS,
    SCORE_COLUMN_MAP,
    ensure_headers,
    extend_table_to_stability_cols,
    get_header_map,
    load_workbook_from_path,
    migrate_stability_columns,
    read_existing_scores,
    read_manual_fields,
    read_prev_composite,
    read_years,
    should_update,
    update_stability,
    write_manual_fields,
)
from manual import apply_manual_entry, prompt_unknown_years
from scoring import (
    NormalisedScores,
    RawScores,
    compute_all_composites,
    normalise_all,
    normalise_column,
    compute_composite,
    compute_global_anchors,
    format_source_years,
    resolve_year,
)
from scraper.http import RateLimiter, titles_match, years_differ
from scraper.gemini_resolver import GeminiResolver, ID_KEYS as GEMINI_ID_KEYS
from scraper.letterboxd_scraper import get_letterboxd_data, get_letterboxd_data_with_slug
from scraper.metacritic_scraper import get_metacritic_data, get_metacritic_data_with_slug
from scraper.omdb_client import get_omdb_data, get_omdb_data_with_id

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pass 1: Fetch all raw scores (with Gemini retry for failures)
# ---------------------------------------------------------------------------

def _gemini_match_ok(source: str, gemini_id: str, title: str,
                     expected_year: Optional[int], result: dict) -> bool:
    """
    Check that the page a Gemini-supplied ID/slug points to is the movie we
    asked about: same title, and release year within tolerance of the
    expected one.  Gemini can invent IDs (e.g. an IMDb ID for an unrelated
    TV episode), and an unchecked one would put another film's score in the
    workbook.
    """
    found_title, found_year = result.get("title"), result.get("year")
    if found_title is None:
        return False  # not found, nothing to use
    if not titles_match(found_title, title):
        logger.warning(
            "Gemini: rejected %s '%s' for '%s' — it's '%s' (%s)",
            source, gemini_id, title, found_title, found_year,
        )
        return False
    if expected_year is not None and years_differ(found_year, expected_year):
        logger.warning(
            "Gemini: rejected %s '%s' for '%s' — it's from %s, expected %s",
            source, gemini_id, title, found_year, expected_year,
        )
        return False
    return True


def fetch_all(
    movies: list[str],
    api_key: str,
    delay: float = 1.0,
    verbose: bool = False,
    resolver=None,
    rate_limiter=None,
    years: Optional[dict] = None,
) -> tuple[list[RawScores], list[str]]:
    """
    Two-pass fetch: first run all scrapers, then retry failed ones with Gemini-resolved slugs.

    Pass 1: Run all scrapers for all movies (no Gemini).
    Pass 2: For movies some source couldn't find at all, ask Gemini (told the
            year) for just those sources' IDs/slugs, verify each answer, and
            retry only those scrapers.  Gemini runs at most once per movie.

    years maps title -> release year (from the optional Year column); a known
    year disambiguates films that share a title.

    Returns:
        (raw_scores: list[RawScores], failed: list[str])
        where failed contains titles of movies that still failed after retry.
    """
    years = years or {}
    raw_scores = []
    failed = []
    failed_for_retry: dict = {}  # title -> Gemini ID keys for sources that didn't find it

    for title in tqdm(movies, desc="Fetching scores (pass 1)", unit="movie"):
        year = years.get(title)
        logger.info("Fetching: %s%s", title, f" ({year})" if year else "")
        try:
            omdb = get_omdb_data(title, api_key, year=year, resolver=None, rate_limiter=rate_limiter)
            time.sleep(delay)

            mc = get_metacritic_data(title, year=year, resolver=None, rate_limiter=rate_limiter)
            time.sleep(delay)

            lb = get_letterboxd_data(title, year=year, resolver=None, rate_limiter=rate_limiter)
            time.sleep(delay)

            scraped_metascore = mc.get("metascore")
            omdb_metascore = omdb.get("metascore") if omdb.get("imdb_id") else None
            metascore = scraped_metascore if scraped_metascore is not None else omdb_metascore

            source_years = {
                "Metacritic": mc.get("year"),
                "Letterboxd": lb.get("year"),
                "OMDb": omdb.get("year"),
            }
            resolved_year = resolve_year(year, source_years)
            if year is None and resolved_year is None and any(source_years.values()):
                logger.warning(
                    "Year: sources matched different films for '%s' (%s) — "
                    "set the Year column to pick one",
                    title, format_source_years(source_years),
                )

            raw_scores.append(RawScores(
                title=title,
                metascore=metascore,
                imdb_rating=omdb.get("imdb_rating"),
                review_count=mc.get("review_count", 0),
                letterboxd_rating=lb.get("rating"),
                year=resolved_year,
                source_years=source_years,
            ))

            if resolver is not None:
                # Only ask Gemini about sources that couldn't find the film.
                # A found page with no score yet (a new release with no IMDB
                # rating on OMDb, say) isn't something a better ID can fix.
                not_found = set()
                if omdb.get("imdb_id") is None:
                    not_found.add("imdb_id")
                if mc.get("url") is None:
                    not_found.add("metacritic_slug")
                if lb.get("url") is None:
                    not_found.add("letterboxd_slug")
                if not_found:
                    failed_for_retry[title] = not_found

        except Exception as exc:
            logger.error("Failed to fetch scores for '%s': %s", title, exc)
            failed.append(title)
            if resolver is not None:
                failed_for_retry[title] = set(GEMINI_ID_KEYS)
            continue

    if failed_for_retry and resolver is not None:
        logger.info("Asking Gemini about %d movie(s) not found on every source", len(failed_for_retry))

        raw_scores_index = {r.title: i for i, r in enumerate(raw_scores)}

        for title, not_found in tqdm(list(failed_for_retry.items()),
                                     desc="Retrying with Gemini", unit="movie"):
            try:
                existing_idx = raw_scores_index.get(title)
                if existing_idx is None:
                    existing_idx = len(raw_scores)
                    raw_scores.append(RawScores(
                        title=title,
                        metascore=None,
                        imdb_rating=None,
                        review_count=0,
                        letterboxd_rating=None,
                    ))
                    raw_scores_index[title] = existing_idx

                existing = raw_scores[existing_idx]
                source_years = dict(existing.source_years)
                # Year to tell Gemini and to check its answers against: the
                # user's, else what the other sources agree on (None = unknown).
                expected_year = resolve_year(years.get(title), source_years)

                gemini_ids = resolver.resolve_all_ids(title, year=expected_year, want=not_found)

                def accept(source: str, key: str, result: dict) -> bool:
                    if _gemini_match_ok(source, gemini_ids[key], title, expected_year, result):
                        logger.info("Gemini: using %s '%s' for '%s' — verified '%s' (%s)",
                                    source, gemini_ids[key], title,
                                    result.get("title"), result.get("year"))
                        return True
                    if result.get("title") is not None:
                        resolver.mark_rejected(title, expected_year, key, gemini_ids[key])
                    return False

                imdb_rating = existing.imdb_rating
                if gemini_ids["imdb_id"]:
                    omdb = get_omdb_data_with_id(api_key, gemini_ids["imdb_id"], rate_limiter=rate_limiter)
                    if accept("OMDb", "imdb_id", omdb):
                        imdb_rating = omdb.get("imdb_rating")
                        source_years["OMDb"] = omdb.get("year")

                metascore = existing.metascore
                review_count = existing.review_count
                if gemini_ids["metacritic_slug"]:
                    mc = get_metacritic_data_with_slug(gemini_ids["metacritic_slug"], rate_limiter=rate_limiter)
                    if accept("Metacritic", "metacritic_slug", mc):
                        metascore = mc.get("metascore") if mc.get("metascore") is not None else metascore
                        review_count = mc.get("review_count", 0)
                        source_years["Metacritic"] = mc.get("year")

                letterboxd_rating = existing.letterboxd_rating
                if gemini_ids["letterboxd_slug"]:
                    lb = get_letterboxd_data_with_slug(gemini_ids["letterboxd_slug"], rate_limiter=rate_limiter)
                    if accept("Letterboxd", "letterboxd_slug", lb):
                        letterboxd_rating = lb.get("rating")
                        source_years["Letterboxd"] = lb.get("year")

                raw_scores[existing_idx] = RawScores(
                    title=title,
                    metascore=metascore,
                    imdb_rating=imdb_rating,
                    review_count=review_count,
                    letterboxd_rating=letterboxd_rating,
                    year=resolve_year(years.get(title), source_years),
                    source_years=source_years,
                )

            except Exception as exc:
                logger.error("Failed to retry '%s' with Gemini: %s", title, exc)
                if title not in failed:
                    failed.append(title)
                continue

    return raw_scores, failed


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def _field_present(raw: RawScores, field: str) -> bool:
    value = getattr(raw, field)
    return value > 0 if field == "review_count" else value is not None


def _scraped_fields(raw: RawScores) -> set:
    """Column names (Metacritic, Reviews, Letterboxd, IMDB) a source supplied for *raw*."""
    return {col for col, field in MANUAL_FIELDS.items() if _field_present(raw, field)}


def _fill_from_existing(raw: RawScores, prev: Optional[RawScores]) -> RawScores:
    """Fill scores the fetch didn't return with the workbook's existing values."""
    if prev is None:
        return raw
    return dataclasses.replace(
        raw,
        metascore=raw.metascore if raw.metascore is not None else prev.metascore,
        imdb_rating=raw.imdb_rating if raw.imdb_rating is not None else prev.imdb_rating,
        letterboxd_rating=raw.letterboxd_rating if raw.letterboxd_rating is not None else prev.letterboxd_rating,
        review_count=raw.review_count or prev.review_count,
    )


def _changed_fields(before: Optional[RawScores], after: RawScores) -> set:
    """Column names whose score differs between *before* and *after* (i.e. typed in)."""
    if before is None:
        return _scraped_fields(after)
    return {
        col for col, field in MANUAL_FIELDS.items()
        if _field_present(after, field) and getattr(after, field) != getattr(before, field)
    }


def update_workbook(
    input_path: Path,
    output_path: Path,
    api_key: str,
    limit: Optional[int] = None,
    target_movie: Optional[str] = None,
    delay: float = 1.0,
    verbose: bool = False,
    smart_update: bool = False,
    manual: bool = False,
    gemini_key: Optional[str] = None,
    random_order: bool = False,
    rate_limit: bool = True,
):
    """
    Three-pass pipeline:
      Pass 1 - fetch_all: fetch raw scores for all movies
      Pass 2 - normalise_all: column-wide min-max normalisation
      Pass 3 - compute_all_composites: compute composite scores
    Then write results to output workbook.
    """
    resolver = None
    if gemini_key:
        try:
            resolver = GeminiResolver(
                api_key=gemini_key,
                cache_path=Path(input_path).parent / ".gemini_cache.json",
            )
            logger.info("Gemini resolver enabled for slug disambiguation")
        except Exception as exc:
            logger.warning("Could not initialise Gemini resolver: %s", exc)

    rate_limiter = None
    if rate_limit:
        rate_limiter = RateLimiter(base_delay=delay, max_delay=30.0)
        logger.info("Rate limiter enabled with base delay %.1fs", delay)

    wb, ws = load_workbook_from_path(input_path)
    header_map = get_header_map(ws)

    title_col = header_map.get("Movies")
    if title_col is None:
        logger.error("Could not find 'Movies' column in %s", input_path)
        sys.exit(1)

    header_map = ensure_headers(ws, header_map)
    header_map = migrate_stability_columns(ws, header_map)

    today = date.today()

    movie_rows = []
    for row in ws.iter_rows(min_row=2, values_only=False):
        title_cell = row[title_col - 1]
        title = title_cell.value
        if title is None or str(title).strip() == "":
            continue
        movie_rows.append((title_cell.row, str(title).strip()))

    years = read_years(ws, header_map, movie_rows)

    if target_movie:
        movie_rows = [(r, t) for r, t in movie_rows if t == target_movie]
        if not movie_rows:
            logger.error("Movie '%s' not found in spreadsheet.", target_movie)
            sys.exit(1)

    if random_order:
        random.shuffle(movie_rows)

    if limit:
        if not random_order:
            random.shuffle(movie_rows)
        movie_rows = movie_rows[:limit]

    if smart_update:
        skipped = []
        filtered_rows = []
        for ws_row, title in movie_rows:
            if should_update(ws, ws_row, header_map, today):
                filtered_rows.append((ws_row, title))
            else:
                skipped.append(title)
        if skipped:
            logger.info(
                "Smart-update: skipping %d stable movie(s): %s",
                len(skipped), ", ".join(skipped),
            )
        movie_rows = filtered_rows

    if not movie_rows:
        logger.info("Nothing to update.")
        extend_table_to_stability_cols(ws)
        return _save_workbook(wb, output_path)

    movies = [t for _, t in movie_rows]

    existing_scores: dict = {}
    for ws_row, title in movie_rows:
        prev = read_existing_scores(ws, ws_row, header_map)
        prev.title = title
        existing_scores[title] = prev

    raw_scores, failed = fetch_all(
        movies, api_key=api_key, delay=delay, verbose=verbose,
        resolver=resolver, rate_limiter=rate_limiter, years=years,
    )

    prompt_scores = manual
    if manual:
        entered_years, interrupted = prompt_unknown_years(raw_scores)
        if entered_years:
            years.update(entered_years)
            refetched, _ = fetch_all(
                list(entered_years), api_key=api_key, delay=delay, verbose=verbose,
                resolver=resolver, rate_limiter=rate_limiter, years=entered_years,
            )
            by_title = {r.title: r for r in refetched}
            raw_scores = [
                by_title.get(r.title, dataclasses.replace(r, year=entered_years.get(r.title, r.year)))
                for r in raw_scores
            ]
        # Ctrl-C during the year prompts stops all prompting, not just years.
        prompt_scores = not interrupted

    # Which scores a source supplied this run — these replace hand-typed ones.
    scraped_by_title = {r.title: _scraped_fields(r) for r in raw_scores}

    # A score a source didn't return this run keeps its workbook value (typed
    # in earlier, or scraped on a past run): it counts in the composite, and
    # --manual doesn't ask for it again.
    raw_scores = [_fill_from_existing(r, existing_scores.get(r.title)) for r in raw_scores]
    before_manual = {r.title: r for r in raw_scores}

    raw_scores, failed, manual_unchanged = apply_manual_entry(
        raw_scores, failed, manual=prompt_scores, existing=existing_scores
    )
    year_by_title = {r.title: r.year for r in raw_scores}

    # Which scores were typed in just now, recorded in the Manual column.
    entered_by_title = {
        r.title: _changed_fields(before_manual.get(r.title) or existing_scores.get(r.title), r)
        for r in raw_scores
    }

    fetched_titles = {r.title for r in raw_scores}

    # Pass 2: normalise across ALL movies in the workbook (not just fetched ones)
    # so that min-max scaling uses the full distribution.
    all_movie_rows_full = []
    for row in ws.iter_rows(min_row=2, values_only=False):
        title_cell = row[title_col - 1]
        t = title_cell.value
        if t is None or str(t).strip() == "":
            continue
        all_movie_rows_full.append((title_cell.row, str(t).strip()))

    scores_lookup = {r.title: r for r in raw_scores}
    full_raw: list[RawScores] = []
    for ws_row_i, title_i in all_movie_rows_full:
        if title_i in scores_lookup:
            full_raw.append(scores_lookup[title_i])
        else:
            existing = read_existing_scores(ws, ws_row_i, header_map)
            existing.title = title_i
            full_raw.append(existing)

    normalised = normalise_all(full_raw)

    # Pass 3: compute composite scores
    final_scores = compute_all_composites(normalised)

    scores_by_title = {ns.title: ns for ns in final_scores}

    for ws_row, title in movie_rows:
        if title not in fetched_titles:
            continue
        ns = scores_by_title.get(title)
        if ns is None:
            continue

        # Read before writing — update_stability needs the old value for comparison.
        prev_comp = read_prev_composite(ws, ws_row, header_map)

        for col_name, field_name in SCORE_COLUMN_MAP.items():
            value = getattr(ns, field_name)
            if value is None:
                continue
            col_idx = header_map.get(col_name)
            if col_idx:
                ws.cell(row=ws_row, column=col_idx, value=value)

        # Record the release year the scores were matched to, so a wrong film
        # is easy to spot.  Unknown years leave the cell as it was.
        year = year_by_title.get(title)
        if year is not None and header_map.get("Year"):
            ws.cell(row=ws_row, column=header_map["Year"], value=year)

        # A scraped score replaces a typed one; newly typed scores are added.
        manual_fields = read_manual_fields(ws, ws_row, header_map)
        manual_fields -= scraped_by_title.get(title, set())
        manual_fields |= entered_by_title.get(title, set())
        write_manual_fields(ws, ws_row, header_map, manual_fields)

        is_unchanged = title in manual_unchanged
        update_stability(ws, ws_row, header_map, ns.composite, prev_comp, today, manual_unchanged=is_unchanged)

    extend_table_to_stability_cols(ws)

    saved_path = _save_workbook(wb, output_path)
    logger.info("Saved updated workbook to %s", saved_path)

    if failed:
        logger.warning("Failed to fetch scores for %d movie(s):", len(failed))
        for t in failed:
            logger.warning("  - %s", t)

    return saved_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch latest Metacritic / Letterboxd / IMDB scores and update Movies.xlsx"
    )
    parser.add_argument(
        "--input", default="Movies.xlsx",
        help="Path to the input Excel file (default: Movies.xlsx)"
    )
    parser.add_argument(
        "--output", default=None,
        help="Path for the output Excel file (default: <input_stem>_updated.xlsx)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Pick N movies at random to process (useful for testing)"
    )
    parser.add_argument(
        "--movie", default=None,
        help="Only update a single movie by exact title"
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Seconds to wait between requests to each source (default: 1.0)"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--api-key", default=None, dest="api_key",
        help=(
            "OMDb API key (overrides OMDB_API_KEY env var). "
            "Prefer setting OMDB_API_KEY in your environment or .env file — "
            "keys passed as CLI arguments are visible in shell history and process listings."
        )
    )
    parser.add_argument(
        "--smart-update", action="store_true", dest="smart_update",
        help=(
            "Skip movies whose scores have been stable recently. "
            "A movie stable for N weeks is skipped for N weeks. "
            "Movies with a blank or hand-typed score are always re-checked, "
            "so a source that adds the film later is picked up. "
            "Reads the input workbook, so run it on the previous output."
        )
    )
    parser.add_argument(
        "--manual", action="store_true", dest="manual",
        help=(
            "Prompt for unknown release years, then for scores that are blank in "
            "the workbook and couldn't be fetched. Fields already filled (typed "
            "in or scraped on an earlier run) aren't asked again. "
            "Ctrl-C stops the prompting and saves everything entered so far."
        )
    )
    parser.add_argument(
        "--gemini-key", default=None, dest="gemini_key",
        help=(
            "Gemini API key for slug disambiguation (overrides GEMINI_API_KEY env var). "
            "When provided, Gemini is used as a last-resort fallback when Metacritic, "
            "Letterboxd, and OMDb cannot find a movie by title. "
            "Prefer setting GEMINI_API_KEY in your environment or .env file — "
            "keys passed as CLI arguments are visible in shell history and process listings."
        )
    )
    parser.add_argument(
        "--random", action="store_true", dest="random",
        help=(
            "Process movies in random order. "
            "When combined with --limit, shuffles first then picks N movies."
        )
    )
    parser.add_argument(
        "--no-rate-limit", action="store_true",
        help="Disable adaptive rate limiting (use fixed delay only)"
    )
    parser.add_argument(
        "--no-open", action="store_true", dest="no_open",
        help=(
            "Don't open the output workbook when the run finishes "
            "(it opens in your default spreadsheet app otherwise)."
        )
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    api_key = args.api_key or os.environ.get("OMDB_API_KEY")
    if not api_key:
        logger.error(
            "No OMDb API key provided. Set OMDB_API_KEY environment variable "
            "or pass --api-key."
        )
        sys.exit(1)
    if args.api_key:
        logger.warning(
            "OMDb API key passed via --api-key. "
            "Prefer setting OMDB_API_KEY in your environment or .env file to "
            "keep it out of shell history and process listings."
        )

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("Input file not found: %s", input_path)
        sys.exit(1)

    output_path = Path(args.output) if args.output else (
        input_path.parent / f"{input_path.stem}_updated{input_path.suffix}"
    )

    logger.info("Input:  %s", input_path)
    logger.info("Output: %s", output_path)

    # Excel locks an open workbook on Windows. Fail now rather than after
    # minutes of fetching and manual entry.
    if _is_locked(output_path):
        logger.error(
            "%s is open in another program (probably Excel). Close it and run again.",
            output_path,
        )
        sys.exit(1)

    gemini_key = args.gemini_key or os.environ.get("GEMINI_API_KEY")
    if args.gemini_key:
        logger.warning(
            "Gemini API key passed via --gemini-key. "
            "Prefer setting GEMINI_API_KEY in your environment or .env file to "
            "keep it out of shell history and process listings."
        )

    saved_path = update_workbook(
        input_path=input_path,
        output_path=output_path,
        api_key=api_key,
        limit=args.limit,
        target_movie=args.movie,
        delay=args.delay,
        verbose=args.verbose,
        smart_update=args.smart_update,
        manual=args.manual,
        gemini_key=gemini_key,
        random_order=args.random,
        rate_limit=not args.no_rate_limit,
    )

    if not args.no_open:
        open_in_default_app(saved_path if isinstance(saved_path, Path) else output_path)


def _is_locked(path: Path) -> bool:
    """True when *path* exists but can't be opened for writing (e.g. open in Excel)."""
    path = Path(path)
    if not path.exists():
        return False
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def _save_workbook(wb, output_path: Path) -> Path:
    """
    Save *wb* to *output_path*.  If that file is locked (opened in Excel
    mid-run), save to a timestamped copy next to it instead so nothing
    fetched or typed in is lost.  Returns the path actually written.
    """
    output_path = Path(output_path)
    try:
        wb.save(output_path)
        return output_path
    except PermissionError:
        fallback = output_path.with_name(
            f"{output_path.stem}_{datetime.now():%Y%m%d-%H%M%S}{output_path.suffix}"
        )
        wb.save(fallback)
        logger.error(
            "%s is open in another program, so results were saved to %s instead. "
            "Close the original and copy this file over it.",
            output_path, fallback,
        )
        return fallback


def open_in_default_app(path: Path) -> None:
    """
    Open *path* with the OS default app for .xlsx (Excel, Numbers, …).

    Skipped when the file doesn't exist; any failure (no default app, no
    desktop session) is logged and never fails the run.
    """
    if not Path(path).exists():
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Opened %s", path)
    except Exception as exc:
        logger.warning("Could not open %s automatically (%s)", path, exc)


if __name__ == "__main__":
    main()
