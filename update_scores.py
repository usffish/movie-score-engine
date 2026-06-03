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
import logging
import os
import random
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from tqdm import tqdm

from excel import (
    SCORE_COLUMN_MAP,
    ensure_headers,
    extend_table_to_stability_cols,
    get_header_map,
    load_workbook_from_path,
    migrate_stability_columns,
    read_existing_scores,
    read_prev_composite,
    should_update,
    update_stability,
)
from manual import apply_manual_entry
from scoring import (
    NormalisedScores,
    RawScores,
    compute_all_composites,
    normalise_all,
    normalise_column,
    compute_composite,
    compute_global_anchors,
)
from scraper.http import RateLimiter
from scraper.gemini_resolver import GeminiResolver
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

def fetch_all(
    movies: list[str],
    api_key: str,
    delay: float = 1.0,
    verbose: bool = False,
    resolver=None,
    rate_limiter=None,
) -> tuple[list[RawScores], list[str]]:
    """
    Two-pass fetch: first run all scrapers, then retry failed ones with Gemini-resolved slugs.

    Pass 1: Run all scrapers for all movies (no Gemini).
    Pass 2: For movies that failed, use Gemini to resolve slugs, then retry only the
            failed scrapers.  This ensures Gemini only runs once per failed movie.

    Returns:
        (raw_scores: list[RawScores], failed: list[str])
        where failed contains titles of movies that still failed after retry.
    """
    raw_scores = []
    failed = []
    failed_for_retry = []

    for title in tqdm(movies, desc="Fetching scores (pass 1)", unit="movie"):
        logger.info("Fetching: %s", title)
        try:
            omdb = get_omdb_data(title, api_key, resolver=None, rate_limiter=rate_limiter)
            time.sleep(delay)

            mc = get_metacritic_data(title, resolver=None, rate_limiter=rate_limiter)
            time.sleep(delay)

            lb = get_letterboxd_data(title, resolver=None, rate_limiter=rate_limiter)
            time.sleep(delay)

            scraped_metascore = mc.get("metascore")
            omdb_metascore = omdb.get("metascore") if omdb.get("imdb_id") else None
            metascore = scraped_metascore if scraped_metascore is not None else omdb_metascore

            raw_scores.append(RawScores(
                title=title,
                metascore=metascore,
                imdb_rating=omdb.get("imdb_rating"),
                review_count=mc.get("review_count", 0),
                letterboxd_rating=lb.get("rating"),
            ))

            if resolver is not None:
                omdb_failed = omdb.get("imdb_rating") is None
                mc_failed = mc.get("review_count", 0) == 0 and metascore is None
                lb_failed = lb.get("rating") is None
                if omdb_failed or mc_failed or lb_failed:
                    failed_for_retry.append(title)

        except Exception as exc:
            logger.error("Failed to fetch scores for '%s': %s", title, exc)
            failed.append(title)
            if resolver is not None:
                failed_for_retry.append(title)
            continue

    if failed_for_retry and resolver is not None:
        logger.info("Retrying %d movie(s) with Gemini slug resolution", len(failed_for_retry))

        raw_scores_index = {r.title: i for i, r in enumerate(raw_scores)}

        for title in tqdm(failed_for_retry, desc="Retrying with Gemini", unit="movie"):
            logger.info("Resolving slugs for: %s", title)
            try:
                gemini_ids = resolver.resolve_all_ids(title)
                gemini_metacritic_slug = gemini_ids["metacritic_slug"]
                gemini_letterboxd_slug = gemini_ids["letterboxd_slug"]
                gemini_imdb_id = gemini_ids["imdb_id"]

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

                if existing.imdb_rating is None and gemini_imdb_id:
                    omdb = get_omdb_data_with_id(api_key, gemini_imdb_id, rate_limiter=rate_limiter)
                    imdb_rating = omdb.get("imdb_rating")
                else:
                    imdb_rating = existing.imdb_rating

                if existing.metascore is None and existing.review_count == 0 and gemini_metacritic_slug:
                    mc = get_metacritic_data_with_slug(gemini_metacritic_slug, rate_limiter=rate_limiter)
                    metascore = mc.get("metascore")
                    review_count = mc.get("review_count", 0)
                else:
                    metascore = existing.metascore
                    review_count = existing.review_count

                if existing.letterboxd_rating is None and gemini_letterboxd_slug:
                    lb = get_letterboxd_data_with_slug(gemini_letterboxd_slug, rate_limiter=rate_limiter)
                    letterboxd_rating = lb.get("rating")
                else:
                    letterboxd_rating = existing.letterboxd_rating

                raw_scores[existing_idx] = RawScores(
                    title=title,
                    metascore=metascore,
                    imdb_rating=imdb_rating,
                    review_count=review_count,
                    letterboxd_rating=letterboxd_rating,
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
            resolver = GeminiResolver(api_key=gemini_key)
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
        wb.save(output_path)
        return

    movies = [t for _, t in movie_rows]

    existing_scores: dict = {}
    if manual:
        for ws_row, title in movie_rows:
            prev = read_existing_scores(ws, ws_row, header_map)
            prev.title = title
            existing_scores[title] = prev

    raw_scores, failed = fetch_all(
        movies, api_key=api_key, delay=delay, verbose=verbose,
        resolver=resolver, rate_limiter=rate_limiter,
    )

    raw_scores, failed, manual_unchanged = apply_manual_entry(
        raw_scores, failed, manual=manual, existing=existing_scores
    )

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

        is_unchanged = title in manual_unchanged
        update_stability(ws, ws_row, header_map, ns.composite, prev_comp, today, manual_unchanged=is_unchanged)

    extend_table_to_stability_cols(ws)

    wb.save(output_path)
    logger.info("Saved updated workbook to %s", output_path)

    if failed:
        logger.warning("Failed to fetch scores for %d movie(s):", len(failed))
        for t in failed:
            logger.warning("  - %s", t)


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
            "Movies with missing scores are always updated."
        )
    )
    parser.add_argument(
        "--manual", action="store_true", dest="manual",
        help=(
            "Prompt for manual entry when scores cannot be fetched automatically. "
            "Existing values in the workbook are preserved when a field is skipped."
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

    gemini_key = args.gemini_key or os.environ.get("GEMINI_API_KEY")
    if args.gemini_key:
        logger.warning(
            "Gemini API key passed via --gemini-key. "
            "Prefer setting GEMINI_API_KEY in your environment or .env file to "
            "keep it out of shell history and process listings."
        )

    update_workbook(
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


if __name__ == "__main__":
    main()
