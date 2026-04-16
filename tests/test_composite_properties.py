"""
Property-based tests for composite score functions.

Design reference:
  Property 3 — Composite formula correctness
    **Validates: Requirements 6.3, 6.10**
  Property 4 — Composite score never raises ZeroDivisionError
    **Validates: Requirements 6.4, 6.5, 6.6, 6.7, 6.8, 6.9**
  Property 5 — Global anchor computation
    **Validates: Requirements 6.1, 6.2**
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from update_scores import (
    NormalisedScores,
    compute_composite,
    compute_global_anchors,
)

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Normalised scores are in [0.0, 1.0]
norm_score = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# Review count is a non-negative integer; use a reasonable upper bound
review_count = st.integers(min_value=1, max_value=10_000)

# Optional normalised score (None or a value in [0.0, 1.0])
optional_norm = st.one_of(st.none(), norm_score)

# Optional review count (None or non-negative int)
optional_reviews = st.one_of(st.none(), st.integers(min_value=0, max_value=10_000))


def _make_normalised_scores(
    title="Test",
    st_metacritic=None,
    review_count=0,
    st_letterboxd=None,
    st_imdb=None,
) -> NormalisedScores:
    """Helper to build a NormalisedScores instance with sensible defaults."""
    return NormalisedScores(
        title=title,
        metascore=None,
        st_metacritic=st_metacritic,
        review_count=review_count,
        letterboxd_rating=None,
        st_letterboxd=st_letterboxd,
        imdb_rating=None,
        st_imdb=st_imdb,
        composite=None,
    )


# ---------------------------------------------------------------------------
# Property 3: Composite formula correctness
# **Validates: Requirements 6.3, 6.10**
# ---------------------------------------------------------------------------

@given(
    st_meta=norm_score,
    reviews=review_count,
    st_lb=norm_score,
    st_imdb=norm_score,
    global_max=norm_score,
    global_min=norm_score,
)
@settings(max_examples=100)
def test_property3_formula_correctness_all_present(
    st_meta, reviews, st_lb, st_imdb, global_max, global_min
):
    """
    Property 3: When all inputs are non-None and reviews > 0, the result
    equals ((st_meta * reviews) + st_lb + global_max + global_min + st_imdb)
    / (reviews + 4), rounded to 2 decimal places.

    **Validates: Requirements 6.3, 6.10**
    """
    result = compute_composite(st_meta, reviews, st_lb, st_imdb, global_max, global_min)

    expected_numerator = (st_meta * reviews) + st_lb + global_max + global_min + st_imdb
    expected_denominator = reviews + 4
    expected = round(expected_numerator / expected_denominator, 2)

    assert result == expected, (
        f"Formula mismatch: got {result}, expected {expected} "
        f"(st_meta={st_meta}, reviews={reviews}, st_lb={st_lb}, "
        f"st_imdb={st_imdb}, global_max={global_max}, global_min={global_min})"
    )


# ---------------------------------------------------------------------------
# Property 4: Composite score never raises ZeroDivisionError
# **Validates: Requirements 6.4, 6.5, 6.6, 6.7, 6.8, 6.9**
# ---------------------------------------------------------------------------

@given(
    st_meta=optional_norm,
    reviews=optional_reviews,
    st_lb=optional_norm,
    st_imdb=optional_norm,
    global_max=optional_norm,
    global_min=optional_norm,
)
@settings(max_examples=100)
def test_property4_no_zero_division_error(
    st_meta, reviews, st_lb, st_imdb, global_max, global_min
):
    """
    Property 4: For any combination of None/non-None inputs, compute_composite
    never raises ZeroDivisionError and returns None when the effective
    denominator would be zero.

    **Validates: Requirements 6.4, 6.5, 6.6, 6.7, 6.8, 6.9**
    """
    # Normalise reviews: treat None as 0
    effective_reviews = reviews if reviews is not None else 0

    try:
        result = compute_composite(
            st_meta, effective_reviews, st_lb, st_imdb, global_max, global_min
        )
    except ZeroDivisionError:
        raise AssertionError(
            f"ZeroDivisionError raised for inputs: "
            f"st_meta={st_meta}, reviews={effective_reviews}, st_lb={st_lb}, "
            f"st_imdb={st_imdb}, global_max={global_max}, global_min={global_min}"
        )

    # Compute the effective denominator independently to verify None is returned
    # when it would be zero
    denom = 0
    if st_meta is not None and effective_reviews:
        denom += effective_reviews
    if st_lb is not None:
        denom += 1
    if global_max is not None and global_min is not None:
        denom += 2
    if st_imdb is not None:
        denom += 1

    if denom == 0:
        assert result is None, (
            f"Expected None when effective denominator is 0, got {result} "
            f"(st_meta={st_meta}, reviews={effective_reviews}, st_lb={st_lb}, "
            f"st_imdb={st_imdb}, global_max={global_max}, global_min={global_min})"
        )
    else:
        assert result is not None, (
            f"Expected a float result when denominator={denom}, got None "
            f"(st_meta={st_meta}, reviews={effective_reviews}, st_lb={st_lb}, "
            f"st_imdb={st_imdb}, global_max={global_max}, global_min={global_min})"
        )


# ---------------------------------------------------------------------------
# Property 5: Global anchor computation
# **Validates: Requirements 6.1, 6.2**
# ---------------------------------------------------------------------------

# Strategy for a list of NormalisedScores with optional None values
normalised_scores_list = st.lists(
    st.builds(
        _make_normalised_scores,
        st_metacritic=optional_norm,
        review_count=st.integers(min_value=0, max_value=500),
        st_letterboxd=optional_norm,
        st_imdb=optional_norm,
    ),
    min_size=0,
    max_size=30,
)


@given(normalised=normalised_scores_list)
@settings(max_examples=100)
def test_property5_global_anchors_are_max_and_min(normalised):
    """
    Property 5: For any list of NormalisedScores, Global_Max_St equals the
    maximum and Global_Min_St equals the minimum of all non-None values across
    st_metacritic, st_letterboxd, and st_imdb combined.

    **Validates: Requirements 6.1, 6.2**
    """
    global_max, global_min = compute_global_anchors(normalised)

    # Collect all non-None values across the three columns
    all_values = []
    for row in normalised:
        for field in (row.st_metacritic, row.st_letterboxd, row.st_imdb):
            if field is not None:
                all_values.append(field)

    if not all_values:
        assert global_max is None, f"Expected None for global_max with no values, got {global_max}"
        assert global_min is None, f"Expected None for global_min with no values, got {global_min}"
    else:
        expected_max = max(all_values)
        expected_min = min(all_values)
        assert global_max == expected_max, (
            f"global_max={global_max} != expected {expected_max}"
        )
        assert global_min == expected_min, (
            f"global_min={global_min} != expected {expected_min}"
        )
