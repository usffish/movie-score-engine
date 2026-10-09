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

import pytest

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


# ---------------------------------------------------------------------------
# Going back a field
# ---------------------------------------------------------------------------

def test_back_returns_to_previous_field():
    # Metascore 90, then "b" at IMDB goes back and 80 replaces it.
    inputs = ["90", "b", "80", "", "", "", ""]
    updated, _, _ = run(inputs, [empty("A")])
    assert updated == [RawScores("A", 80, None, 0, None)]


def test_back_at_first_field_stays_put(capsys):
    inputs = ["b", "90", "", "", "", ""]
    updated, _, _ = run(inputs, [empty("A")])
    assert "Already at the first field" in capsys.readouterr().out
    assert updated[0].metascore == 90


def test_back_past_metascore_drops_the_review_count():
    # Back from Letterboxd to Metascore, which is then skipped: the review
    # count typed alongside it goes too, and isn't asked again.
    inputs = ["90", "7.0", "12", "b", "b", "b", "", "7.5", "", ""]
    updated, _, _ = run(inputs, [empty("A")])
    assert updated == [RawScores("A", None, 7.5, 0, None)]


# ---------------------------------------------------------------------------
# Fixing a title
# ---------------------------------------------------------------------------

def run_with_retitle(inputs, raw_scores, retitle, failed=None, existing=None):
    with patch("builtins.input", side_effect=inputs):
        return apply_manual_entry(
            raw_scores, failed or [], manual=True, existing=existing, retitle=retitle
        )


def test_title_fix_refetches_and_skips_prompts_when_complete(capsys):
    calls = []

    def retitle(old, new):
        calls.append((old, new))
        return RawScores(new, 70, 7.0, 20, 3.5)

    updated, failed, _ = run_with_retitle(["t", "Alpha"], [], retitle, failed=["Alpah"])
    assert calls == [("Alpah", "Alpha")]
    assert updated == [RawScores("Alpha", 70, 7.0, 20, 3.5)]
    assert failed == []
    assert "nothing left to enter" in capsys.readouterr().out


def test_title_fix_asks_only_for_what_the_refetch_missed():
    retitle = lambda old, new: RawScores(new, 70, None, 20, 3.5)
    updated, _, _ = run_with_retitle(["t", "Alpha", "7.2", ""], [empty("Alpah")], retitle)
    assert updated == [RawScores("Alpha", 70, 7.2, 20, 3.5)]


def test_title_fix_that_still_fails_is_reported_under_the_new_title():
    updated, failed, _ = run_with_retitle(
        ["t", "Alpha", "", "", ""], [], lambda old, new: None, failed=["Alpah"]
    )
    assert updated == []
    assert failed == ["Alpha"]


def test_title_fix_rejected_keeps_prompting(capsys):
    def retitle(old, new):
        raise ValueError(f"'{new}' is already in the workbook.")

    updated, failed, _ = run_with_retitle(
        ["t", "Beta", "75", "", "", "", ""], [], retitle, failed=["Alpah"]
    )
    assert "already in the workbook" in capsys.readouterr().out
    assert updated == [RawScores("Alpah", 75, None, 0, None)]


def test_title_fix_cancelled_with_enter():
    retitle = lambda old, new: pytest.fail("retitle should not be called")
    updated, _, _ = run_with_retitle(["t", "", "75", "", "", "", ""], [empty("A")], retitle)
    assert updated[0].metascore == 75


def test_t_is_not_a_command_without_retitle(capsys):
    updated, _, _ = run(["t", "", "", ""], [empty("A")])
    out = capsys.readouterr().out
    assert "Invalid Metascore" in out
    assert "fixes the title" not in out


# ---------------------------------------------------------------------------
# Review before saving
# ---------------------------------------------------------------------------

def test_review_lists_typed_scores(capsys):
    run(["90", "", "12", "", ""], [empty("A")])
    out = capsys.readouterr().out
    assert "Review: typed scores for 1 movie(s)" in out
    assert "A: Metascore 90 · Reviews 12" in out


def test_review_redo_replaces_the_entry():
    inputs = ["90", "", "", "", "70", "", "", "",   # A, then B
              "1",                                  # redo A
              "85", "", "", "",
              ""]                                   # save
    updated, _, _ = run(inputs, [empty("A"), empty("B")])
    assert [r.metascore for r in updated] == [85, 70]


def test_review_discard_drops_typed_scores():
    updated, failed, _ = run(["90", "", "", "", "75", "", "", "", "d"], [empty("A")], failed=["X"])
    assert updated == [empty("A")]
    assert failed == ["X"]


def test_review_invalid_choice_asks_again(capsys):
    updated, _, _ = run(["90", "", "", "", "9", ""], [empty("A")])
    assert "Enter a number from 1 to 1" in capsys.readouterr().out
    assert updated[0].metascore == 90


def test_review_skipped_when_nothing_typed(capsys):
    run(["", "", ""], [empty("A")])
    assert "Review" not in capsys.readouterr().out


def test_review_end_of_input_saves():
    updated, _, _ = run(["90", "", "", "", EOFError()], [empty("A")])
    assert updated[0].metascore == 90
