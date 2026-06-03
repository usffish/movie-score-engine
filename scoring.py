"""
scoring.py
==========
Data models and scoring math: normalisation and composite calculation.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class RawScores:
    title: str
    metascore: Optional[int]           # 0-100; 50 when OMDb returns N/A
    imdb_rating: Optional[float]       # 0.0-10.0; None when N/A
    review_count: int                  # >= 0; 0 when Metacritic not found
    letterboxd_rating: Optional[float] # 0.0-5.0; None when not found


@dataclass
class NormalisedScores:
    title: str
    metascore: Optional[int]
    st_metacritic: Optional[float]   # 0.0-1.0 or None
    review_count: int
    letterboxd_rating: Optional[float]
    st_letterboxd: Optional[float]   # 0.0-1.0 or None
    imdb_rating: Optional[float]
    st_imdb: Optional[float]         # 0.0-1.0 or None
    composite: Optional[float]       # 0.0-1.0 or None, rounded to 2dp


def normalise_column(values: list[Optional[float]]) -> list[Optional[float]]:
    """
    Apply min-max normalisation to a column of values.

    - Returns 0.0 for all entries when max == min (flat column).
    - Returns None for entries where the input is None.
    - min and max are computed over non-None values only.
    """
    non_none = [v for v in values if v is not None]
    if not non_none:
        return [None if v is None else 0.0 for v in values]

    col_min = min(non_none)
    col_max = max(non_none)

    if col_max == col_min:
        return [None if v is None else 0.0 for v in values]

    return [
        None if v is None else (v - col_min) / (col_max - col_min)
        for v in values
    ]


def normalise_all(raw_scores: list[RawScores]) -> list[NormalisedScores]:
    """
    Pass 2: apply normalise_column to Metacritic, Letterboxd, and IMDB columns.

    Returns a list of NormalisedScores with composite set to None (computed in Pass 3).
    """
    meta_col = [float(r.metascore) if r.metascore is not None else None for r in raw_scores]
    lb_col = [r.letterboxd_rating for r in raw_scores]
    imdb_col = [r.imdb_rating for r in raw_scores]

    norm_meta = normalise_column(meta_col)
    norm_lb = normalise_column(lb_col)
    norm_imdb = normalise_column(imdb_col)

    return [
        NormalisedScores(
            title=raw.title,
            metascore=raw.metascore,
            st_metacritic=norm_meta[i],
            review_count=raw.review_count,
            letterboxd_rating=raw.letterboxd_rating,
            st_letterboxd=norm_lb[i],
            imdb_rating=raw.imdb_rating,
            st_imdb=norm_imdb[i],
            composite=None,
        )
        for i, raw in enumerate(raw_scores)
    ]


def compute_global_anchors(normalised: list[NormalisedScores]) -> tuple[Optional[float], Optional[float]]:
    """
    Compute (Global_Max_St, Global_Min_St) across all non-None normalised values.

    Returns (None, None) when there are no non-None values.
    """
    all_values = [
        field
        for row in normalised
        for field in (row.st_metacritic, row.st_letterboxd, row.st_imdb)
        if field is not None
    ]
    if not all_values:
        return (None, None)
    return (max(all_values), min(all_values))


def compute_composite(
    st_meta: Optional[float],
    reviews: int,
    st_lb: Optional[float],
    st_imdb: Optional[float],
    global_max: Optional[float],
    global_min: Optional[float],
) -> Optional[float]:
    """
    Compute composite score with dynamic denominator.

    Formula (full):
        ((st_meta x reviews) + st_lb + global_max + global_min + st_imdb)
        / (reviews + 4)

    Dynamic adjustments:
        - reviews == 0 or None  -> drop st_meta x reviews term; base denom = 4
        - st_lb is None         -> drop st_lb; denom -= 1
        - st_imdb is None       -> drop st_imdb; denom -= 1
        - global_max or global_min is None -> drop both anchor terms; denom -= 2
        - effective denom == 0  -> return None
    """
    numerator = 0.0
    denominator = 0

    if st_meta is not None and reviews:
        numerator += st_meta * reviews
        denominator += reviews

    if st_lb is not None:
        numerator += st_lb
        denominator += 1

    if global_max is not None and global_min is not None:
        numerator += global_max + global_min
        denominator += 2

    if st_imdb is not None:
        numerator += st_imdb
        denominator += 1

    if denominator == 0:
        return None

    return round(numerator / denominator, 2)


def compute_all_composites(normalised: list[NormalisedScores]) -> list[NormalisedScores]:
    """
    Pass 3: compute composite scores for all movies.

    Calls compute_global_anchors once, then compute_composite per movie.
    Returns a new list of NormalisedScores with the composite field populated.
    """
    global_max, global_min = compute_global_anchors(normalised)

    return [
        NormalisedScores(
            title=row.title,
            metascore=row.metascore,
            st_metacritic=row.st_metacritic,
            review_count=row.review_count,
            letterboxd_rating=row.letterboxd_rating,
            st_letterboxd=row.st_letterboxd,
            imdb_rating=row.imdb_rating,
            st_imdb=row.st_imdb,
            composite=compute_composite(
                st_meta=row.st_metacritic,
                reviews=row.review_count,
                st_lb=row.st_letterboxd,
                st_imdb=row.st_imdb,
                global_max=global_max,
                global_min=global_min,
            ),
        )
        for row in normalised
    ]
