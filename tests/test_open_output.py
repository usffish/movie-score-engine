"""
Tests for opening the output workbook at the end of a run, and for the
guards against the output being locked (open in Excel).

Covers:
- main() opens the saved workbook unless --no-open is given
- the opener uses the platform's default-app mechanism, skips missing files,
  and never fails the run
- a locked output file stops the run before any fetching
- if the output becomes locked mid-run, results go to a timestamped copy
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import openpyxl
import pytest

import update_scores
from update_scores import _save_workbook, main, open_in_default_app


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "Movies.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["Movies"])
    wb.active.append(["Buddy"])
    wb.save(path)
    return path


def _run_main(book, *extra):
    output = book.with_name("Movies_updated.xlsx")

    def fake_update(**kwargs):
        openpyxl.Workbook().save(kwargs["output_path"])
        return kwargs["output_path"]

    with patch.dict(os.environ, {"OMDB_API_KEY": "k"}), \
         patch("update_scores.update_workbook", side_effect=fake_update), \
         patch("update_scores.open_in_default_app") as opener:
        main(["--input", str(book), "--output", str(output), *extra])
    return opener, output


def test_output_opened_at_end(book):
    opener, output = _run_main(book)
    opener.assert_called_once_with(output)


def test_no_open_flag(book):
    opener, _ = _run_main(book, "--no-open")
    opener.assert_not_called()


def test_opener_uses_platform_default(tmp_path):
    f = tmp_path / "out.xlsx"
    f.write_bytes(b"x")
    with patch.object(update_scores.sys, "platform", "win32"), \
         patch.object(update_scores.os, "startfile", create=True) as startfile:
        open_in_default_app(f)
    startfile.assert_called_once_with(str(f))

    with patch.object(update_scores.sys, "platform", "darwin"), \
         patch("update_scores.subprocess.Popen") as popen:
        open_in_default_app(f)
    popen.assert_called_once_with(["open", str(f)])


def test_opener_skips_missing_file(tmp_path):
    with patch("update_scores.subprocess.Popen") as popen, \
         patch.object(update_scores.os, "startfile", create=True) as startfile:
        open_in_default_app(tmp_path / "missing.xlsx")
    popen.assert_not_called()
    startfile.assert_not_called()


def test_opener_failure_is_not_fatal(tmp_path, caplog):
    f = tmp_path / "out.xlsx"
    f.write_bytes(b"x")
    with patch.object(update_scores.sys, "platform", "linux"), \
         patch("update_scores.subprocess.Popen", side_effect=FileNotFoundError("xdg-open")):
        open_in_default_app(f)  # must not raise
    assert "Could not open" in caplog.text


def test_locked_output_stops_before_fetching(book):
    output = book.with_name("Movies_updated.xlsx")
    output.write_bytes(b"x")
    with patch.dict(os.environ, {"OMDB_API_KEY": "k"}), \
         patch("update_scores._is_locked", return_value=True), \
         patch("update_scores.update_workbook") as uw, \
         pytest.raises(SystemExit) as exc:
        main(["--input", str(book), "--output", str(output)])
    assert exc.value.code == 1
    uw.assert_not_called()


def test_locked_mid_run_saves_timestamped_copy(tmp_path, caplog):
    wb = MagicMock()
    target = tmp_path / "Movies_updated.xlsx"
    wb.save.side_effect = [PermissionError("locked"), None]
    saved = _save_workbook(wb, target)
    assert saved != target
    assert saved.parent == target.parent
    assert saved.name.startswith("Movies_updated_") and saved.suffix == ".xlsx"
    assert wb.save.call_args_list[1].args[0] == saved
    assert "saved to" in caplog.text


def test_unlocked_file_is_not_reported_locked(tmp_path):
    f = tmp_path / "out.xlsx"
    f.write_bytes(b"x")
    assert update_scores._is_locked(f) is False
    assert update_scores._is_locked(tmp_path / "missing.xlsx") is False
