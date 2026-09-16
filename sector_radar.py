"""日米セクターの相対的な勢いと出来高を確認する画面。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st


# 判定条件はここでまとめて変更できます。
# 順位変化は「20日前の順位 - 5日前の順位」で、プラスほど順位上昇です。
THRESHOLDS = {
    "relative_5d": 0.0,
    "rank_gain": 3,
    "return_5d": 0.0,
    "volume_ratio": 1.2,
    "hot_score": 4,
    "watch_score": 3,
}

JP_NAMES = [
    "食品",
    "エネルギー資源",
    "建設・資材",
    "素材・化学",
    "医薬品",
    "自動車・輸送機",
    "鉄鋼・非鉄",
    "機械",
    "電機・精密",
    "情報通信・サービスその他",
    "電力・ガス",
    "運輸・物流",
    "商社・卸売",
    "小売",
    "銀行",
    "金融（除く銀行）",
    "不動産",
]

# 日本はTOPIX-17 ETF、米国はセクターSPDRを比較します。
UNIVERSES = {
    "日本": ("1306.T", {f"{1617 + i}.T": name for i, name in enumerate(JP_NAMES)}),
    "米国": (
        "SPY",
        {
            "XLB": "素材",
            "XLC": "通信サービス",
            "XLE": "エネルギー",
            "XLF": "金融",
            "XLI": "資本財・サービス",
            "XLK": "情報技術",
            "XLP": "生活必需品",
            "XLRE": "不動産",
            "XLU": "公益事業",
            "XLV": "ヘルスケア",
            "XLY": "一般消費財",
        },
    ),
}

COLUMNS = [
    "Signal",
    "Sector",
    "1D",
    "5D",
    "20D",
    "対市場5D",
    "対市場20D",
    "5D順位",
    "20D順位",
    "順位変化",
    "出来高比率",
]


def fetch_history(symbol: str) -> tuple[pd.DataFrame, str | None]:
    """Yahoo Financeから確定済みの日足価格と出来高を取得します。"""
    try:
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}",
            params={"range": "6mo", "interval": "1d"},
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (Investment Dashboard)"},
        )
        response.raise_for_status()
        chart = response.json()["chart"]
        if chart.get("error") or not chart.get("result"):
            raise ValueError("データなし")

        raw = chart["result"][0]
        timestamps = raw.get("timestamp", [])
        quote_data = raw["indicators"]["quote"][0]
        adjusted = raw["indicators"].get("adjclose", [{}])[0].get("adjclose")
        if adjusted is None:
            raise ValueError("調整後終値なし")

        try:
            market_timezone = ZoneInfo(raw["meta"].get("exchangeTimezoneName", "UTC"))
        except Exception:
            market_timezone = ZoneInfo("UTC")
        dates = (
            pd.to_datetime(timestamps, unit="s", utc=True)
            .tz_convert(market_timezone)
            .tz_localize(None)
            .normalize()
        )
        volumes = quote_data.get("volume") or [None] * len(timestamps)
        frame = pd.DataFrame(
            {"price": adjusted, "volume": volumes},
            index=dates,
        )
        frame = frame.apply(pd.to_numeric, errors="coerce").replace(
            [np.inf, -np.inf], np.nan
        )
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()

        # 取引中の価格・出来高を判定に混ぜないため、現地の当日足を除外します。
        today = pd.Timestamp(datetime.now(market_timezone).date())
        frame = frame.loc[frame.index < today]
        frame.loc[frame["price"] <= 0, "price"] = np.nan
        frame.loc[frame["volume"] < 0, "volume"] = np.nan
        if frame["price"].notna().sum() < 21:
            raise ValueError("21営業日分の履歴不足")
        return frame, None
    except Exception as exc:
        message = f"{symbol}: {type(exc).__name__}: {str(exc)[:140]}"
        return pd.DataFrame(), message


@st.cache_data(ttl=1800, show_spinner=False)
def load_market(
    market: str,
) -> tuple[dict[str, tuple[pd.DataFrame, str | None]], str]:
    """対象市場を並行取得し、30分間キャッシュします。"""
    benchmark, sectors = UNIVERSES[market]
    symbols = [benchmark, *sectors]
    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = list(pool.map(fetch_history, symbols))
    fetched_at = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d %H:%M JST")
    return dict(zip(symbols, fetched)), fetched_at


def calculate(
    data: dict[str, tuple[pd.DataFrame, str | None]],
    benchmark: str,
    sectors: dict[str, str],
) -> tuple[pd.DataFrame, dict[str, pd.Series], list[str], pd.Timestamp | None]:
    """同じ21営業日を使って騰落率、相対値、順位、Signalを計算します。"""
    base = data[benchmark][0]
    if base.empty:
        message = "市場ETFを取得できないため、相対値・Signalを計算できません。"
        return pd.DataFrame(), {}, [message], None

    dates = base.dropna(subset=["price"]).index[-21:]
    if len(dates) < 21:
        return pd.DataFrame(), {}, ["市場ETFの履歴不足"], None
    today_jst = pd.Timestamp(datetime.now(ZoneInfo("Asia/Tokyo")).date())
    if (today_jst - dates[-1]).days > 7:
        message = "市場ETFのデータが7日超古いため、判定を停止しました。"
        return pd.DataFrame(), {}, [message], None

    benchmark_prices = base["price"].reindex(dates)
    rows: list[dict] = []
    charts: dict[str, pd.Series] = {}
    excluded: list[str] = []

    for symbol, name in sectors.items():
        frame, error = data[symbol]
        label = f"{name} ({symbol})"
        if frame.empty:
            excluded.append(error or label)
            continue

        prices = frame["price"].reindex(dates)
        # 穴埋めせず、比較市場と同じ21本が揃ったセクターだけを順位付けします。
        if prices.isna().any():
            excluded.append(f"{label}: 市場と同日の21本が揃いません")
            continue

        returns = {
            days: (prices.iloc[-1] / prices.iloc[-1 - days] - 1) * 100
            for days in (1, 5, 20)
        }
        volumes = frame["volume"].reindex(dates)
        previous_mean = (
            volumes.iloc[:-1].mean() if volumes.iloc[:-1].notna().all() else np.nan
        )
        volume_ratio = (
            volumes.iloc[-1] / previous_mean
            if pd.notna(previous_mean) and previous_mean > 0
            else np.nan
        )
        rows.append(
            {
                "Sector": label,
                "1D": returns[1],
                "5D": returns[5],
                "20D": returns[20],
                "対市場5D": returns[5]
                - (benchmark_prices.iloc[-1] / benchmark_prices.iloc[-6] - 1) * 100,
                "対市場20D": returns[20]
                - (benchmark_prices.iloc[-1] / benchmark_prices.iloc[0] - 1) * 100,
                "出来高比率": volume_ratio,
            }
        )
        relative = prices / benchmark_prices
        charts[label] = (relative / relative.iloc[0] - 1) * 100

    if not rows:
        return pd.DataFrame(), charts, excluded, dates[-1]

    table = pd.DataFrame(rows)
    table["5D順位"] = table["5D"].rank(ascending=False, method="min").astype(int)
    table["20D順位"] = table["20D"].rank(ascending=False, method="min").astype(int)
    table["順位変化"] = table["20D順位"] - table["5D順位"]

    checks = [
        table["対市場5D"] > THRESHOLDS["relative_5d"],
        table["順位変化"] >= THRESHOLDS["rank_gain"],
        table["5D"] > THRESHOLDS["return_5d"],
        table["出来高比率"] >= THRESHOLDS["volume_ratio"],
    ]
    table["_score"] = sum(check.astype(int) for check in checks)
    table["Signal"] = np.where(
        table["_score"] >= THRESHOLDS["hot_score"],
        "🔥 急浮上",
        np.where(
            table["_score"] >= THRESHOLDS["watch_score"],
            "👀 監視",
            "通常",
        ),
    )
    return table, charts, excluded, dates[-1]


def render_sector_radar() -> None:
    """Sector Radarの操作欄、一覧、相対強度チャートを表示します。"""
    st.divider()
    st.header("🧭 Sector Radar")
    st.caption(
        "価格と出来高から、相対的に勢いが強まったセクターを探します。"
        "実際の資金流入額を測るものではありません。"
    )
    market = st.radio("市場", list(UNIVERSES), horizontal=True, key="radar_market")
    order = st.selectbox(
        "並べ替え", ["Signal順", "5日順位順"], key="radar_order"
    )
    benchmark, sectors = UNIVERSES[market]

    with st.spinner("セクターデータを取得中（30分キャッシュ）…"):
        data, fetched_at = load_market(market)
    table, charts, excluded, date = calculate(data, benchmark, sectors)

    st.subheader("急浮上セクター TOP5")
    if table.empty:
        st.info("判定できるデータがありません。時間を置いて再度お試しください。")
    else:
        top = table[table["Signal"] == "🔥 急浮上"].sort_values(
            ["順位変化", "対市場5D"], ascending=False
        ).head(5)
        if top.empty:
            st.info("現在、急浮上の条件を満たすセクターはありません。")
        else:
            for _, row in top.iterrows():
                st.write(
                    f"🔥 **{row['Sector']}** — 順位 +{row['順位変化']} / "
                    f"対市場5D {row['対市場5D']:+.2f}pt"
                )

        st.caption(
            f"判定日: {date:%Y/%m/%d} ｜ 比較市場: {benchmark} ｜ "
            f"対象: {len(table)}/{len(sectors)}業種 ｜ 取得: {fetched_at}"
        )
        if order == "Signal順":
            sorted_table = table.sort_values(
                ["_score", "順位変化", "対市場5D"], ascending=False
            )
        else:
            sorted_table = table.sort_values("5D順位")

        column_config = {
            column: st.column_config.NumberColumn(column, format="%.2f%%")
            for column in ["1D", "5D", "20D"]
        }
        column_config.update(
            {
                column: st.column_config.NumberColumn(column, format="%.2f pt")
                for column in ["対市場5D", "対市場20D"]
            }
        )
        column_config["出来高比率"] = st.column_config.NumberColumn(
            "出来高比率", format="%.2f倍"
        )
        st.dataframe(
            sorted_table[COLUMNS],
            hide_index=True,
            width="stretch",
            column_config=column_config,
        )
        selected = st.selectbox(
            "相対強度チャート", list(charts), key="radar_chart"
        )
        st.line_chart(charts[selected].rename("対市場相対強度 (%)"))
        st.caption(
            "チャートは20営業日前の『セクターETF価格÷市場ETF価格』を"
            "0%として表示します。"
        )

    if excluded:
        st.warning(
            "取得失敗・欠損のある業種を除外しています。"
            "順位は取得できた業種内の順位です。"
        )
        with st.expander("Sector Radarの取得状況"):
            for message in excluded:
                st.text(message)

    with st.expander("判定条件・データ仕様"):
        st.write(
            f"🔥 急浮上: {THRESHOLDS['hot_score']}条件以上、"
            f"👀 監視: {THRESHOLDS['watch_score']}条件以上。"
        )
        st.write(
            f"対市場5D > {THRESHOLDS['relative_5d']}pt / "
            f"順位上昇 ≥ {THRESHOLDS['rank_gain']} / "
            f"5D > {THRESHOLDS['return_5d']}% / "
            f"出来高比率 ≥ {THRESHOLDS['volume_ratio']}倍。"
        )
        st.write(
            "現地の当日足は除外し、調整後終値で計算します。"
            "対市場は騰落率の差、出来高比率は最終日の出来高÷その前20営業日の平均です。"
        )
        st.write(
            "日本: TOPIX-17 ETF（1617〜1633）、比較市場: TOPIX ETF 1306。"
            "米国: 11セクターSPDR、比較市場: SPY。"
        )
        st.caption(
            "ETFの出来高は業種全体の出来高とは異なり、"
            "薄商いで比率が大きくなる場合があります。"
        )
        st.markdown(
            "[日本ETF仕様](https://nextfunds.jp/lineup/1617/) · "
            "[米国セクターETF仕様](https://www.ssga.com/us/en/intermediary/"
            "capabilities/equities/sector-investing/sector-and-industry-etfs)"
        )
