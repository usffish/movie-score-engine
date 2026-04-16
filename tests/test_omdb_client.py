"""
Unit tests for scraper/omdb_client.py.

Covers:
- "N/A" Metascore → 50
- "N/A" imdbRating → None
- Response: "False" → fallback values
- Retry count exactly 3 on network failure
- year parameter appears in request URL
- API key appears in request URL
"""

import unittest
from unittest.mock import MagicMock, patch

import requests

from scraper.omdb_client import get_omdb_data, _parse_metascore, _parse_imdb_rating


def _make_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Helper: build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.url = "http://www.omdbapi.com/?t=test&apikey=key"
    return resp


class TestParseMetascore(unittest.TestCase):
    def test_valid_integer_string(self):
        self.assertEqual(_parse_metascore("75"), 75)

    def test_na_returns_50(self):
        self.assertEqual(_parse_metascore("N/A"), 50)

    def test_none_returns_50(self):
        self.assertEqual(_parse_metascore(None), 50)

    def test_empty_string_returns_50(self):
        self.assertEqual(_parse_metascore(""), 50)

    def test_zero_string(self):
        self.assertEqual(_parse_metascore("0"), 0)

    def test_hundred_string(self):
        self.assertEqual(_parse_metascore("100"), 100)


class TestParseImdbRating(unittest.TestCase):
    def test_valid_decimal_string(self):
        self.assertAlmostEqual(_parse_imdb_rating("8.5"), 8.5)

    def test_na_returns_none(self):
        self.assertIsNone(_parse_imdb_rating("N/A"))

    def test_none_returns_none(self):
        self.assertIsNone(_parse_imdb_rating(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_imdb_rating(""))

    def test_zero_string(self):
        self.assertAlmostEqual(_parse_imdb_rating("0.0"), 0.0)

    def test_ten_string(self):
        self.assertAlmostEqual(_parse_imdb_rating("10.0"), 10.0)


class TestGetOmdbData(unittest.TestCase):

    def _patch_session_get(self, side_effect=None, return_value=None):
        """Patch SESSION.get and return the patcher + mock."""
        patcher = patch("scraper.omdb_client.SESSION.get")
        mock_get = patcher.start()
        if side_effect is not None:
            mock_get.side_effect = side_effect
        elif return_value is not None:
            mock_get.return_value = return_value
        return patcher, mock_get

    # ------------------------------------------------------------------
    # N/A Metascore → 50
    # ------------------------------------------------------------------
    def test_na_metascore_returns_50(self):
        resp = _make_response({
            "Response": "True",
            "Metascore": "N/A",
            "imdbRating": "7.5",
            "imdbID": "tt1234567",
        })
        patcher, _ = self._patch_session_get(return_value=resp)
        try:
            result = get_omdb_data("Some Movie", "testkey")
            self.assertEqual(result["metascore"], 50)
            self.assertAlmostEqual(result["imdb_rating"], 7.5)
        finally:
            patcher.stop()

    # ------------------------------------------------------------------
    # N/A imdbRating → None
    # ------------------------------------------------------------------
    def test_na_imdb_rating_returns_none(self):
        resp = _make_response({
            "Response": "True",
            "Metascore": "80",
            "imdbRating": "N/A",
            "imdbID": "tt1234567",
        })
        patcher, _ = self._patch_session_get(return_value=resp)
        try:
            result = get_omdb_data("Some Movie", "testkey")
            self.assertEqual(result["metascore"], 80)
            self.assertIsNone(result["imdb_rating"])
        finally:
            patcher.stop()

    # ------------------------------------------------------------------
    # Response: "False" → fallback values
    # ------------------------------------------------------------------
    def test_response_false_returns_fallbacks(self):
        resp = _make_response({
            "Response": "False",
            "Error": "Movie not found!",
        })
        patcher, _ = self._patch_session_get(return_value=resp)
        try:
            result = get_omdb_data("Unknown Movie", "testkey")
            self.assertEqual(result["metascore"], 50)
            self.assertIsNone(result["imdb_rating"])
            self.assertIsNone(result["imdb_id"])
        finally:
            patcher.stop()

    # ------------------------------------------------------------------
    # Retry count exactly 3 on network failure
    # ------------------------------------------------------------------
    def test_retry_exactly_3_times_on_network_error(self):
        patcher = patch("scraper.omdb_client.SESSION.get")
        mock_get = patcher.start()
        mock_get.side_effect = requests.ConnectionError("connection refused")

        sleep_patcher = patch("scraper.omdb_client.time.sleep")
        sleep_patcher.start()

        try:
            result = get_omdb_data("Some Movie", "testkey")
            # Should have attempted exactly 3 times
            self.assertEqual(mock_get.call_count, 3)
            # Should return fallbacks after exhaustion
            self.assertEqual(result["metascore"], 50)
            self.assertIsNone(result["imdb_rating"])
            self.assertIsNone(result["imdb_id"])
        finally:
            patcher.stop()
            sleep_patcher.stop()

    # ------------------------------------------------------------------
    # year parameter appears in request params
    # ------------------------------------------------------------------
    def test_year_parameter_included_when_provided(self):
        resp = _make_response({
            "Response": "True",
            "Metascore": "70",
            "imdbRating": "7.0",
            "imdbID": "tt9999999",
        })
        patcher, mock_get = self._patch_session_get(return_value=resp)
        try:
            get_omdb_data("Blade Runner", "testkey", year=1982)
            call_kwargs = mock_get.call_args
            params = call_kwargs[1].get("params") or call_kwargs[0][1]
            self.assertIn("y", params)
            self.assertEqual(params["y"], 1982)
        finally:
            patcher.stop()

    def test_year_parameter_absent_when_not_provided(self):
        resp = _make_response({
            "Response": "True",
            "Metascore": "70",
            "imdbRating": "7.0",
            "imdbID": "tt9999999",
        })
        patcher, mock_get = self._patch_session_get(return_value=resp)
        try:
            get_omdb_data("Blade Runner", "testkey")
            call_kwargs = mock_get.call_args
            params = call_kwargs[1].get("params") or call_kwargs[0][1]
            self.assertNotIn("y", params)
        finally:
            patcher.stop()

    # ------------------------------------------------------------------
    # API key appears in request params
    # ------------------------------------------------------------------
    def test_api_key_included_in_request(self):
        resp = _make_response({
            "Response": "True",
            "Metascore": "60",
            "imdbRating": "6.5",
            "imdbID": "tt0000001",
        })
        patcher, mock_get = self._patch_session_get(return_value=resp)
        try:
            get_omdb_data("Test Film", "my_secret_key")
            call_kwargs = mock_get.call_args
            params = call_kwargs[1].get("params") or call_kwargs[0][1]
            self.assertIn("apikey", params)
            self.assertEqual(params["apikey"], "my_secret_key")
        finally:
            patcher.stop()

    # ------------------------------------------------------------------
    # Successful response returns correct parsed values
    # ------------------------------------------------------------------
    def test_successful_response_parsed_correctly(self):
        resp = _make_response({
            "Response": "True",
            "Metascore": "88",
            "imdbRating": "8.3",
            "imdbID": "tt0118749",
        })
        patcher, _ = self._patch_session_get(return_value=resp)
        try:
            result = get_omdb_data("Boogie Nights", "testkey")
            self.assertEqual(result["metascore"], 88)
            self.assertAlmostEqual(result["imdb_rating"], 8.3)
            self.assertEqual(result["imdb_id"], "tt0118749")
        finally:
            patcher.stop()

    # ------------------------------------------------------------------
    # Non-200 HTTP response triggers retry then fallback
    # ------------------------------------------------------------------
    def test_non_200_response_triggers_retry_and_fallback(self):
        bad_resp = MagicMock()
        bad_resp.status_code = 503
        bad_resp.url = "http://www.omdbapi.com/"

        patcher = patch("scraper.omdb_client.SESSION.get")
        mock_get = patcher.start()
        mock_get.return_value = bad_resp

        sleep_patcher = patch("scraper.omdb_client.time.sleep")
        sleep_patcher.start()

        try:
            result = get_omdb_data("Some Movie", "testkey")
            self.assertEqual(mock_get.call_count, 3)
            self.assertEqual(result["metascore"], 50)
            self.assertIsNone(result["imdb_rating"])
        finally:
            patcher.stop()
            sleep_patcher.stop()

    # ------------------------------------------------------------------
    # Exponential back-off: sleep durations grow between retries
    # ------------------------------------------------------------------
    def test_exponential_backoff_sleep_durations(self):
        patcher = patch("scraper.omdb_client.SESSION.get")
        mock_get = patcher.start()
        mock_get.side_effect = requests.ConnectionError("fail")

        sleep_patcher = patch("scraper.omdb_client.time.sleep")
        mock_sleep = sleep_patcher.start()

        try:
            get_omdb_data("Some Movie", "testkey")
            # With backoff=2.0: sleep(2.0 * 2^0)=2.0, sleep(2.0 * 2^1)=4.0
            # (no sleep after the last attempt)
            sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
            self.assertEqual(len(sleep_calls), 2)
            self.assertLess(sleep_calls[0], sleep_calls[1])
        finally:
            patcher.stop()
            sleep_patcher.stop()


if __name__ == "__main__":
    unittest.main()
