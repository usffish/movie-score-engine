"""
Tests for release-year disambiguation.

Covers:
- Letterboxd tries the year-suffixed slug before the plain slug
- Letterboxd skips a page whose release year doesn't match
- read_years parses the optional Year column
"""

import unittest
from unittest.mock import MagicMock, patch

import openpyxl

from excel import get_header_map, read_years
from scraper.letterboxd_scraper import _candidate_slugs, get_letterboxd_data


def _page(title_with_year: str, rating: float) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = f"""
    <html><head>
    <meta property="og:title" content="{title_with_year}">
    <meta itemprop="ratingValue" content="{rating}">
    </head><body></body></html>
    """
    return resp


def _not_found() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 404
    resp.text = ""
    return resp


class TestCandidateSlugs(unittest.TestCase):

    def test_year_slug_comes_first(self):
        self.assertEqual(
            _candidate_slugs("Parasite", 2019)[:2],
            ["parasite-2019", "parasite"],
        )

    def test_no_year_starts_with_plain_slug(self):
        self.assertEqual(_candidate_slugs("Parasite")[0], "parasite")


class TestLetterboxdYear(unittest.TestCase):

    def _fake_get(self, pages):
        def get(url, **kwargs):
            return pages.get(url, _not_found())
        return get

    def test_year_suffixed_page_is_used(self):
        pages = {
            "https://letterboxd.com/film/parasite-2019/": _page("Parasite (2019)", 4.5),
            "https://letterboxd.com/film/parasite/": _page("Parasite (1982)", 2.4),
        }
        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=self._fake_get(pages)):
            result = get_letterboxd_data("Parasite", year=2019)
        self.assertEqual(result["rating"], 4.5)
        self.assertEqual(result["url"], "https://letterboxd.com/film/parasite-2019/")

    def test_wrong_year_plain_slug_is_skipped(self):
        pages = {
            "https://letterboxd.com/film/parasite/": _page("Parasite (1982)", 2.4),
            "https://letterboxd.com/film/parasite-1/": _page("Parasite (2019)", 4.5),
        }
        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=self._fake_get(pages)):
            result = get_letterboxd_data("Parasite", year=2019)
        self.assertEqual(result["rating"], 4.5)

    def test_plain_slug_used_when_year_matches(self):
        pages = {
            "https://letterboxd.com/film/the-godfather/": _page("The Godfather (1972)", 4.51),
        }
        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=self._fake_get(pages)):
            result = get_letterboxd_data("The Godfather", year=1972)
        self.assertEqual(result["rating"], 4.51)

    def test_no_year_keeps_old_behaviour(self):
        pages = {
            "https://letterboxd.com/film/parasite/": _page("Parasite (1982)", 2.4),
        }
        with patch("scraper.letterboxd_scraper.SESSION.get", side_effect=self._fake_get(pages)):
            result = get_letterboxd_data("Parasite")
        self.assertEqual(result["rating"], 2.4)


class TestReadYears(unittest.TestCase):

    def _sheet(self, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        return ws

    def test_no_year_column_returns_empty(self):
        ws = self._sheet([["Movies"], ["Parasite"]])
        self.assertEqual(read_years(ws, get_header_map(ws), [(2, "Parasite")]), {})

    def test_parses_ints_strings_and_skips_blanks(self):
        ws = self._sheet([
            ["Movies", "Year"],
            ["Parasite", 2019],
            ["Boogie Nights", "1997"],
            ["Unknown", None],
            ["Garbage", "n/a"],
        ])
        rows = [(2, "Parasite"), (3, "Boogie Nights"), (4, "Unknown"), (5, "Garbage")]
        self.assertEqual(
            read_years(ws, get_header_map(ws), rows),
            {"Parasite": 2019, "Boogie Nights": 1997},
        )


if __name__ == "__main__":
    unittest.main()
