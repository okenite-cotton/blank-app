"""通信なしで主要な失敗ケースを確認: python -m unittest -v"""
import base64
from datetime import date, datetime, timezone
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import streamlit_app as app


NOW = datetime(2026, 9, 8, 2, tzinfo=timezone.utc)


def history(days=("2026-09-04", "2026-09-07"), closes=(100, 110)):
    return pd.DataFrame({"Date": list(days), "Close": list(closes)})


def result(days=("2026-09-04", "2026-09-07"), closes=(100, 110)):
    return {"history": app.clean_history(history(days, closes), NOW.date()),
            "source": "Test", "fetched_at": NOW.isoformat(), "url": "https://example.com",
            "fallback": False, "notes": []}


class DashboardTests(unittest.TestCase):
    def test_default_settings_have_five_unique_markets(self):
        settings = app.validate_market_list(app.default_market_list())
        self.assertEqual(len(settings), 5)
        self.assertEqual(len({item["id"] for item in settings}), 5)
        self.assertTrue(all(item["yahoo_jp"].startswith("https://finance.yahoo.co.jp/")
                            for item in settings))

    def test_settings_round_trip_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard_markets.json"
            expected = app.save_market_settings(app.default_market_list(), path)
            loaded, error = app.load_market_settings(path)
        self.assertIsNone(error)
        self.assertEqual(loaded, expected)
        self.assertEqual(loaded[0]["name"], "日経平均")

    def test_broken_settings_fall_back_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard_markets.json"
            path.write_text("not-json", encoding="utf-8")
            loaded, error = app.load_market_settings(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "not-json")
        self.assertEqual(len(loaded), 5)
        self.assertIn("初期設定", error)

    def test_duplicate_symbol_is_rejected(self):
        settings = app.default_market_list()
        duplicate = {**settings[0], "id": "another-nikkei"}
        with self.assertRaises(app.DataError):
            app.validate_market_list([*settings, duplicate])

    def test_custom_market_uses_yahoo_without_optional_stooq(self):
        market = app.normalize_market({"id": "aapl", "name": "Apple", "yahoo": "aapl",
                                       "unit": "USD", "zone": "America/New_York",
                                       "caption": "APPLE", "yahoo_jp": app.yahoo_japan_url("AAPL")})
        with patch.object(app, "read_yahoo", return_value=history()):
            fetched = app.fetch_market("aapl", NOW, market)
        self.assertEqual(fetched["source"], "Yahoo Finance")
        self.assertIn("Yahoo!ファイナンス（日本）で詳細を見る",
                      app.card_html("aapl", fetched, market=market))

    def test_sort_deduplicate_and_reject_invalid_values(self):
        frame = history(("2026-09-07", "2026-09-04", "2026-09-04", "2026-09-09",
                         "bad date", "2026-09-03", "2026-09-02", "2026-09-01"),
                        (110, 95, 100, 999, 100, float("inf"), 0, float("nan")))
        clean = app.clean_history(frame, date(2026, 9, 8))
        self.assertEqual(clean["Close"].tolist(), [100, 110])

    def test_no_valid_prices(self):
        with self.assertRaises(app.DataError):
            app.clean_history(history(closes=(0, -1)), NOW.date())

    def test_latest_two_sorted_days(self):
        self.assertEqual(app.quote_values(result()), (110, 10, 10))

    def test_single_row_has_no_invented_change(self):
        self.assertEqual(app.quote_values(result(("2026-09-07",), (110,))), (110, None, None))

    def test_stooq_descending_csv(self):
        frame = app.parse_stooq_csv("Date,Open,High,Low,Close\n2026-09-07,0,0,0,110\n2026-09-04,0,0,0,100\n")
        self.assertEqual(app.clean_history(frame, NOW.date())["Close"].tolist(), [100, 110])

    def test_stooq_challenge_and_no_data_are_errors(self):
        for text in ("No data", "<!doctype html><html>Verify your browser</html>", "Date,Open\n2026-09-07,1"):
            with self.subTest(text=text), self.assertRaises(app.DataError):
                app.parse_stooq_csv(text)

    def test_topix_uses_column_names_and_ignores_other_numbers(self):
        html = '<h1>TOPIXの時系列</h1><p>9,999.99</p><table><tr><th>終値</th><th>日付</th></tr><tr><td>4,125.80</td><th>2026/9/7</th></tr></table>'
        frame = app.parse_topix_html(html)
        self.assertEqual(float(frame.iloc[0]["Close"]), 4125.8)
        with self.assertRaises(app.DataError):
            app.parse_topix_html(html.replace("TOPIX", "別の指数"))

    def test_topix_changed_page_fails_closed(self):
        with self.assertRaises(app.DataError):
            app.parse_topix_html("<h1>TOPIX</h1><p>4,000.00</p>")

    def test_yahoo_failure_falls_back_without_mixing_series(self):
        with patch.object(app, "read_yahoo", side_effect=app.DataError("unavailable")), \
             patch.object(app, "read_stooq", return_value=history(closes=(200, 220))):
            fetched = app.fetch_market("nikkei", NOW)
        self.assertEqual(fetched["source"], "Stooq")
        self.assertTrue(fetched["fallback"])
        self.assertEqual(app.quote_values(fetched), (220, 20, 10))

    def test_topix_does_not_use_invalid_yfinance_symbol(self):
        with patch.object(app, "read_topix", return_value=history()), \
             patch.object(app, "read_yahoo") as yahoo:
            fetched = app.fetch_market("topix", NOW)
        yahoo.assert_not_called()
        self.assertEqual(fetched["source"], "Yahoo!ファイナンス（日本）")

    def test_topix_failure_also_falls_back(self):
        with patch.object(app, "read_topix", side_effect=app.DataError("changed page")), \
             patch.object(app, "read_stooq", return_value=history()):
            self.assertEqual(app.fetch_market("topix", NOW)["source"], "Stooq")

    def test_old_primary_tries_fallback(self):
        with patch.object(app, "read_yahoo", return_value=history(("2026-08-20", "2026-08-21"))), \
             patch.object(app, "read_stooq", return_value=history()):
            self.assertEqual(app.fetch_market("nikkei", NOW)["source"], "Stooq")

    def test_all_sources_fail_and_saved_value_can_survive(self):
        with patch.object(app, "read_yahoo", side_effect=app.DataError("unavailable")), \
             patch.object(app, "read_stooq", side_effect=app.DataError("unavailable")):
            with self.assertRaises(app.DataError):
                app.fetch_market("nikkei", NOW)
        saved = result()
        self.assertEqual(app.prefer_saved(None, saved), (saved, True))
        self.assertEqual(app.prefer_saved(None, None), (None, False))

    def test_older_fallback_never_replaces_newer_saved_day(self):
        saved = result()
        older = result(("2026-09-03", "2026-09-04"))
        self.assertEqual(app.prefer_saved(older, saved), (saved, True))

    def test_yahoo_keeps_tokyo_date(self):
        frame = pd.DataFrame({"Close": [100, 110]}, index=pd.DatetimeIndex(
            ["2026-09-04 00:00", "2026-09-07 00:00"], tz="Asia/Tokyo"))
        with patch.object(app.yf, "Ticker") as ticker:
            ticker.return_value.history.return_value = frame
            data = app.read_yahoo(app.MARKETS["nikkei"])
        self.assertEqual(data.iloc[-1]["Date"].date(), date(2026, 9, 7))

    def test_http_429_is_not_retried(self):
        with patch.object(app.requests, "get") as get:
            get.return_value.status_code = 429
            with self.assertRaises(app.DataError):
                app.get_text("https://example.com")
        self.assertEqual(get.call_count, 1)

    def test_connection_timeout_has_one_retry(self):
        with patch.object(app.requests, "get", side_effect=app.requests.Timeout), \
             patch.object(app.time, "sleep") as sleep:
            with self.assertRaises(app.DataError):
                app.get_text("https://example.com")
        sleep.assert_called_once()

    def test_neutral_sparkline_and_missing_card(self):
        html = app.sparkline(result(closes=(100, 100))["history"], 0)
        svg = base64.b64decode(re.search(r"base64,([^\"]+)", html).group(1)).decode("utf-8")
        self.assertIn("#bdc9da", svg)
        self.assertIn("取得できません", app.card_html("topix", None))


if __name__ == "__main__":
    unittest.main()
