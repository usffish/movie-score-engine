"""
Property-based tests for normalise_column.

**Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6**

Design reference: Property 1 — Min-max normalisation correctness
  For any list of numeric values (with optional None entries), applying
  min-max normalisation should produce values where:
    (a) every non-None output is in the range [0.0, 1.0]
    (b) the minimum non-None input maps to 0.0
    (c) the maximum non-None input maps to 1.0
    (d) None inputs produce None outputs
    (e) when all non-None values are identical, all outputs are 0.0
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from update_scores import normalise_column


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# A strategy that produces either None or a finite float within a range where
# (max - min) cannot overflow to inf.  Values near ±float_max cause
# (max - min) → inf → nan in the normalisation formula, which is not a
# realistic input for any score column this code will ever process.
# 1e150 gives enormous headroom while keeping arithmetic well-defined.
optional_float = st.one_of(
    st.none(),
    st.floats(min_value=-1e150, max_value=1e150, allow_nan=False, allow_infinity=False),
)

# A non-empty list of optional floats
optional_float_list = st.lists(optional_float, min_size=1, max_size=50)


# ---------------------------------------------------------------------------
# Property 1: Min-max normalisation correctness
# ---------------------------------------------------------------------------

@given(values=optional_float_list)
@settings(max_examples=100)
def test_property1_outputs_in_unit_interval(values):
    """(a) Every non-None output is in [0.0, 1.0]."""
    result = normalise_column(values)
    for v in result:
        if v is not None:
            assert 0.0 <= v <= 1.0, f"Output {v} is outside [0.0, 1.0] for input {values}"


@given(values=optional_float_list)
@settings(max_examples=100)
def test_property1_min_maps_to_zero(values):
    """(b) The minimum non-None input maps to 0.0."""
    non_none_inputs = [v for v in values if v is not None]
    if len(non_none_inputs) < 2:
        return  # Need at least two distinct values for a meaningful min/max check

    col_min = min(non_none_inputs)
    col_max = max(non_none_inputs)
    if col_min == col_max:
        return  # Flat column — covered by sub-property (e)

    result = normalise_column(values)
    for i, v in enumerate(values):
        if v == col_min:
            assert result[i] == 0.0, (
                f"Min input {v} did not map to 0.0 (got {result[i]}) for input {values}"
            )


@given(values=optional_float_list)
@settings(max_examples=100)
def test_property1_max_maps_to_one(values):
    """(c) The maximum non-None input maps to 1.0."""
    non_none_inputs = [v for v in values if v is not None]
    if len(non_none_inputs) < 2:
        return

    col_min = min(non_none_inputs)
    col_max = max(non_none_inputs)
    if col_min == col_max:
        return  # Flat column — covered by sub-property (e)

    result = normalise_column(values)
    for i, v in enumerate(values):
        if v == col_max:
            assert result[i] == 1.0, (
                f"Max input {v} did not map to 1.0 (got {result[i]}) for input {values}"
            )


@given(values=optional_float_list)
@settings(max_examples=100)
def test_property1_none_inputs_produce_none_outputs(values):
    """(d) None inputs produce None outputs."""
    result = normalise_column(values)
    assert len(result) == len(values), "Output length must match input length"
    for i, v in enumerate(values):
        if v is None:
            assert result[i] is None, (
                f"None input at index {i} produced non-None output {result[i]}"
            )


@given(
    base=st.floats(allow_nan=False, allow_infinity=False),
    size=st.integers(min_value=1, max_value=20),
    none_indices=st.lists(st.integers(min_value=0, max_value=19), max_size=10),
)
@settings(max_examples=100)
def test_property1_flat_column_maps_to_zero(base, size, none_indices):
    """(e) When all non-None values are identical, all outputs are 0.0."""
    values = [base] * size
    # Sprinkle in some Nones
    for idx in none_indices:
        if idx < size:
            values[idx] = None

    result = normalise_column(values)
    for i, v in enumerate(values):
        if v is not None:
            assert result[i] == 0.0, (
                f"Flat column: non-None output at index {i} is {result[i]}, expected 0.0"
            )
        else:
            assert result[i] is None, (
                f"Flat column: None input at index {i} produced non-None output {result[i]}"
            )
