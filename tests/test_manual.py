"""
Unit tests for manual.py interactive entry.

Covers:
  - Progress counter shows position, total, and how many are left
  - Ctrl-C during a movie keeps the fields already typed for that movie
  - Ctrl-C stops further prompting without raising
  - Movies not yet prompted are returned unchanged
  - Failed movies remaining after an interrupt stay in the failed list
"""

from unittest.mock import patch

from manual import apply_manual_entry, prompt_failed_movie, prompt_missing_scores
from scoring import RawScores


def empty(title):
    return RawScores(
        title=title, metascore=None, imdb_rating=None,
        review_count=0, letterboxd_rating=None,
    )


def run(inputs, raw_scores, failed=None):
    """Drive apply_manual_entry with a scripted list of stdin responses."""
    with patch("builtins.input", side_effect=inputs):
        return apply_manual_entry(raw_scores, failed or [], manual=True)


# ---------------------------------------------------------------------------
# Progress counter
# ---------------------------------------------------------------------------

def test_progress_counter_shows_position_and_remaining(capsys):
    with patch("builtins.input", return_value=""):
        prompt_missing_scores(empty("A"), position=3, total=12)
    assert "[3/12 · 9 left]" in capsys.readouterr().out


def test_progress_counter_omitted_when_position_unknown(capsys):
    with patch("builtins.input", return_value=""):
        prompt_missing_scores(empty("A"))
    assert "left]" not in capsys.readouterr().out


def test_total_counts_incomplete_movies_and_failed(capsys):
    complete = RawScores(
        title="Complete", metascore=80, imdb_rating=7.5,
        review_count=12, letterboxd_rating=4.0,
    )
    run([""] * 12, [complete, empty("A"), empty("B")], failed=["C"])
    out = capsys.readouterr().out
    # Two incomplete movies + one failed = 3; "Complete" is not prompted for.
    assert "[1/3 · 2 left]" in out
    assert "[2/3 · 1 left]" in out
    assert "[3/3 · 0 left]" in out
    assert "Complete" not in out


def test_failed_movie_prompt_shows_progress(capsys):
    with patch("builtins.input", return_value=""):
        prompt_failed_movie("A", position=2, total=5)
    assert "[2/5 · 3 left]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Interrupt handling
# ---------------------------------------------------------------------------

def test_interrupt_keeps_previously_completed_entries():
    inputs = ["90", "8.1", "40", "4.2", KeyboardInterrupt()]
    updated, failed, _ = run(inputs, [empty("A"), empty("B")])

    assert updated[0] == RawScores("A", 90, 8.1, 40, 4.2)
    assert failed == []


def test_interrupt_keeps_partial_fields_of_current_movie():
    # Second movie: Metascore and IMDB entered, then Ctrl-C.
    inputs = ["90", "8.1", "40", "4.2", "70", "6.5", KeyboardInterrupt()]
    updated, _, _ = run(inputs, [empty("A"), empty("B")])

    assert updated[1] == RawScores("B", 70, 6.5, 0, None)


def test_interrupt_does_not_raise_and_leaves_later_movies_unchanged():
    inputs = [KeyboardInterrupt()]
    updated, _, _ = run(inputs, [empty("A"), empty("B"), empty("C")])

    assert [r.title for r in updated] == ["A", "B", "C"]
    assert updated[1] == empty("B")
    assert updated[2] == empty("C")


def test_interrupt_skips_remaining_failed_movies():
    inputs = [KeyboardInterrupt()]
    updated, failed, _ = run(inputs, [empty("A")], failed=["X", "Y"])

    assert failed == ["X", "Y"]
    assert [r.title for r in updated] == ["A"]


def test_interrupt_during_failed_movie_keeps_partial_entry():
    inputs = ["75", KeyboardInterrupt()]
    updated, failed, _ = run(inputs, [], failed=["X", "Y"])

    assert updated == [RawScores("X", 75, None, 0, None)]
    assert failed == ["Y"]


def test_interrupt_reports_how_many_entries_were_kept(capsys):
    inputs = ["90", "8.1", "40", "4.2", KeyboardInterrupt()]
    run(inputs, [empty("A"), empty("B"), empty("C")])
    assert "stopped at 2 of 3" in capsys.readouterr().out


def test_interrupt_before_any_input_keeps_nothing():
    updated, _, _ = run([KeyboardInterrupt()], [empty("A")])
    assert updated == [empty("A")]
