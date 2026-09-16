import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import sector_radar


def history(prices, *, missing=None):
    end = datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    dates = pd.bdate_range(end=end, periods=len(prices))
    frame = pd.DataFrame(
        {"price": prices, "volume": np.full(len(prices), 1_000.0)},
        index=dates,
    )
    if missing is not None:
        frame = frame.drop(frame.index[missing])
    return frame


class SectorRadarTests(unittest.TestCase):
    def test_all_four_conditions_produce_hot_signal(self):
        benchmark = history(np.linspace(100, 110, 21))
        sector = history(np.linspace(100, 125, 21))
        sector.loc[sector.index[-1], "volume"] = 2_000
        data = {"BASE": (benchmark, None), "A": (sector, None)}
        permissive = {
            "relative_5d": -999,
            "rank_gain": -999,
            "return_5d": -999,
            "volume_ratio": 0,
        }

        with patch.dict(sector_radar.THRESHOLDS, permissive):
            table, charts, excluded, date = sector_radar.calculate(
                data, "BASE", {"A": "テスト業種"}
            )

        self.assertEqual(table.iloc[0]["Signal"], "🔥 急浮上")
        self.assertEqual(int(table.iloc[0]["_score"]), 4)
        self.assertIn("テスト業種 (A)", charts)
        self.assertEqual(excluded, [])
        self.assertIsNotNone(date)

    def test_sector_missing_a_market_session_is_excluded(self):
        benchmark = history(np.linspace(100, 110, 21))
        incomplete = history(np.linspace(100, 120, 21), missing=5)
        data = {"BASE": (benchmark, None), "A": (incomplete, None)}

        table, _, excluded, _ = sector_radar.calculate(
            data, "BASE", {"A": "欠損業種"}
        )

        self.assertTrue(table.empty)
        self.assertEqual(len(excluded), 1)
        self.assertIn("21本が揃いません", excluded[0])

    def test_missing_benchmark_stops_calculation(self):
        data = {"BASE": (pd.DataFrame(), "取得失敗")}

        table, charts, excluded, date = sector_radar.calculate(data, "BASE", {})

        self.assertTrue(table.empty)
        self.assertEqual(charts, {})
        self.assertIn("市場ETFを取得できない", excluded[0])
        self.assertIsNone(date)


if __name__ == "__main__":
    unittest.main()
