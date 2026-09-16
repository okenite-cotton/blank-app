"""５つの指標を確認する投資ダッシュボード。

起動: python -m streamlit run streamlit_app.py --server.port 8502
設定 → データ取得 → 表示の順に読むと、処理の流れが分かります。
"""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from html import escape
from io import StringIO
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import pandas as pd
import requests
import streamlit as st
import yfinance as yf


# ── 1. 設定 ────────────────────────────────────────────────
CACHE_SECONDS = 300                  # 同じデータを５分間再利用
OLD_AFTER_DAYS = 7                    # ７暦日超なら、古い可能性を表示
MAX_HISTORY_DAYS = 120
TOPIX_URL = "https://finance.yahoo.co.jp/quote/998405.T/history"
SETTINGS_PATH = Path(__file__).with_name("dashboard_markets.json")
MAX_MARKETS = 20

DEFAULT_MARKETS = {
    "nikkei": {"name": "日経平均", "yahoo": "^N225", "stooq": "^nkx",
               "unit": "pt", "zone": "Asia/Tokyo", "caption": "NIKKEI 225",
               "yahoo_jp": "https://finance.yahoo.co.jp/quote/998407.O", "enabled": True},
    "topix": {"name": "TOPIX", "yahoo": None, "stooq": "^tpx",
              "unit": "pt", "zone": "Asia/Tokyo", "caption": "東証株価指数",
              "yahoo_jp": "https://finance.yahoo.co.jp/quote/998405.T", "enabled": True,
              "kind": "topix"},
    "usdjpy": {"name": "ドル円", "yahoo": "JPY=X", "stooq": "usdjpy",
               "unit": "円 / 1 USD", "zone": "Europe/London", "caption": "USD / JPY",
               "yahoo_jp": "https://finance.yahoo.co.jp/fx/detail/?code=USDJPY=FX", "enabled": True},
    "sp500": {"name": "S&P500", "yahoo": "^GSPC", "stooq": "^spx",
              "unit": "pt", "zone": "America/New_York", "caption": "S&P 500",
              "yahoo_jp": "https://finance.yahoo.co.jp/quote/%5EGSPC", "enabled": True},
    "nasdaq": {"name": "NASDAQ総合", "yahoo": "^IXIC", "stooq": "^ndq",
               "unit": "pt", "zone": "America/New_York", "caption": "NASDAQ COMPOSITE",
               "yahoo_jp": "https://finance.yahoo.co.jp/quote/%5EIXIC", "enabled": True},
}

# 既存コードやテストから参照しやすい別名。実際の表示内容は保存設定から読みます。
MARKETS = DEFAULT_MARKETS

PRESET_MARKETS = {
    "dow": {"name": "NYダウ", "yahoo": "^DJI", "stooq": "^dji", "unit": "pt",
            "zone": "America/New_York", "caption": "DOW JONES",
            "yahoo_jp": "https://finance.yahoo.co.jp/quote/%5EDJI", "enabled": True},
    "gold": {"name": "金（先物）", "yahoo": "GC=F", "stooq": "gc.f", "unit": "USD",
             "zone": "America/New_York", "caption": "GOLD FUTURES",
             "yahoo_jp": "https://finance.yahoo.co.jp/quote/GC%3DF", "enabled": True},
    "bitcoin": {"name": "ビットコイン", "yahoo": "BTC-USD", "stooq": None, "unit": "USD",
                "zone": "UTC", "caption": "BITCOIN / USD",
                "yahoo_jp": "https://finance.yahoo.co.jp/quote/BTC-USD", "enabled": True},
    "eurjpy": {"name": "ユーロ円", "yahoo": "EURJPY=X", "stooq": "eurjpy", "unit": "円 / 1 EUR",
               "zone": "Europe/London", "caption": "EUR / JPY",
               "yahoo_jp": "https://finance.yahoo.co.jp/fx/detail/?code=EURJPY=FX", "enabled": True},
    "oil": {"name": "原油（WTI先物）", "yahoo": "CL=F", "stooq": "cl.f", "unit": "USD",
            "zone": "America/New_York", "caption": "WTI CRUDE OIL",
            "yahoo_jp": "https://finance.yahoo.co.jp/quote/CL%3DF", "enabled": True},
    "dax": {"name": "ドイツDAX", "yahoo": "^GDAXI", "stooq": "^dax", "unit": "pt",
            "zone": "Europe/Berlin", "caption": "DAX",
            "yahoo_jp": "https://finance.yahoo.co.jp/quote/%5EGDAXI", "enabled": True},
    "hangseng": {"name": "香港ハンセン", "yahoo": "^HSI", "stooq": "^hsi", "unit": "pt",
                 "zone": "Asia/Hong_Kong", "caption": "HANG SENG",
                 "yahoo_jp": "https://finance.yahoo.co.jp/quote/%5EHSI", "enabled": True},
}


class DataError(Exception):
    """画面に表示してよい、データ取得・形式についての説明。"""


def default_market_list():
    """初期の5指標を、保存しやすいリスト形式で返す。"""
    return [{"id": key, **deepcopy(value)} for key, value in DEFAULT_MARKETS.items()]


def yahoo_japan_url(symbol):
    """日本版Yahoo!ファイナンスの詳細ページ候補を作る。"""
    return "https://finance.yahoo.co.jp/quote/" + quote(symbol, safe="")


def normalize_market(raw):
    """保存ファイルや入力欄から来た1件を、安全な共通形式にそろえる。"""
    if not isinstance(raw, dict):
        raise DataError("指標の設定形式が正しくありません")
    market_id = str(raw.get("id", "")).strip()
    name = str(raw.get("name", "")).strip()
    yahoo = raw.get("yahoo")
    yahoo = None if yahoo is None else str(yahoo).strip().upper()
    stooq = raw.get("stooq")
    stooq = None if not stooq else str(stooq).strip().lower()
    kind = str(raw.get("kind", "")).strip() or None
    if not re.fullmatch(r"[a-z0-9_-]{1,50}", market_id):
        raise DataError("設定内のIDが正しくありません")
    if not name or len(name) > 40:
        raise DataError("表示名は1〜40文字で入力してください")
    if kind != "topix" and (not yahoo or not re.fullmatch(r"[A-Z0-9.^=_-]{1,30}", yahoo)):
        raise DataError(f"{name}: Yahooコードが正しくありません")
    if stooq and not re.fullmatch(r"[a-z0-9.^=_-]{1,30}", stooq):
        raise DataError(f"{name}: Stooqコードが正しくありません")
    zone = str(raw.get("zone", "Asia/Tokyo"))
    try:
        ZoneInfo(zone)
    except Exception:
        raise DataError(f"{name}: 地域設定が正しくありません") from None
    unit = str(raw.get("unit", "")).strip()[:30] or "—"
    caption = str(raw.get("caption", "")).strip()[:50] or (yahoo or name)
    jp_url = str(raw.get("yahoo_jp", "")).strip()
    if not jp_url and yahoo:
        jp_url = yahoo_japan_url(yahoo)
    if not jp_url.startswith("https://finance.yahoo.co.jp/"):
        raise DataError(f"{name}: 日本版Yahoo!のリンクが正しくありません")
    return {"id": market_id, "name": name, "yahoo": yahoo, "stooq": stooq,
            "unit": unit, "zone": zone, "caption": caption,
            "yahoo_jp": jp_url, "enabled": bool(raw.get("enabled", True)),
            **({"kind": kind} if kind else {})}


def validate_market_list(items):
    """設定全体を確認し、重複を除いたリストを返す。"""
    if not isinstance(items, list) or not items:
        raise DataError("指標が1件以上必要です")
    if len(items) > MAX_MARKETS:
        raise DataError(f"登録できる指標は{MAX_MARKETS}件までです")
    normalized = [normalize_market(item) for item in items]
    ids = [item["id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise DataError("同じIDの指標が重複しています")
    symbols = [item["yahoo"] for item in normalized if item["yahoo"]]
    if len(symbols) != len(set(symbols)):
        raise DataError("同じYahooコードの指標が重複しています")
    return normalized


def load_market_settings(path=SETTINGS_PATH):
    """保存設定を読み込む。未作成なら初期の5指標を使う。"""
    if not path.exists():
        return default_market_list(), None
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
        return validate_market_list(items), None
    except (OSError, json.JSONDecodeError, DataError) as error:
        return default_market_list(), f"保存設定を読み込めないため、初期設定を表示しています：{error}"


def save_market_settings(items, path=SETTINGS_PATH):
    """途中で壊れたファイルを残さないよう、一時ファイル経由で保存する。"""
    normalized = validate_market_list(items)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        raise DataError(f"設定を保存できませんでした：{error}") from None
    return normalized


def unique_market_id(symbol, items):
    """コードから重複しない短いIDを作る。"""
    base = re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-") or "market"
    existing = {item["id"] for item in items}
    candidate, number = base[:40], 2
    while candidate in existing:
        suffix = f"-{number}"
        candidate = base[:50 - len(suffix)] + suffix
        number += 1
    return candidate


def market_payload(market):
    """キャッシュのキーに使える、並び順が安定したJSON文字列。"""
    return json.dumps(market, ensure_ascii=False, sort_keys=True)


# ── 2. データの読み取りと検証 ──────────────────────────────
def get_text(url, params=None):
    """待ち時間を制限。通信断・5xxだけ１回再試行する。"""
    for attempt in range(2):
        try:
            response = requests.get(url, params=params, timeout=(4, 10))
            # アクセス制限・認証・ブラウザー確認は回避しない。
            if response.status_code in (401, 403, 429):
                raise DataError(f"接続制限（HTTP {response.status_code}）")
            if response.status_code >= 500 and attempt == 0:
                time.sleep(0.5)
                continue
            response.raise_for_status()
            response.encoding = "utf-8-sig"
            return response.text
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 1:
                raise DataError("通信がタイムアウト、または接続できませんでした") from None
            time.sleep(0.5)
    raise DataError("データを取得できませんでした")


def clean_history(frame, today):
    """日付を昇順にし、重複・欠損・無限大・未来の日付を除く。"""
    if not {"Date", "Close"}.issubset(frame.columns):
        raise DataError("日付と終値の列が見つかりません")
    data = frame[["Date", "Close"]].copy()
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce").dt.normalize()
    data["Close"] = pd.to_numeric(data["Close"], errors="coerce")
    data = data.dropna()
    data = data[data["Close"].map(lambda x: math.isfinite(x) and x > 0)]
    data = data[data["Date"].between(pd.Timestamp(today - timedelta(days=MAX_HISTORY_DAYS)),
                                       pd.Timestamp(today))]
    data = data.drop_duplicates("Date", keep="last").sort_values("Date")
    if data.empty:
        raise DataError("有効な価格データがありません")
    return data.reset_index(drop=True)


def read_yahoo(market):
    """Yahoo Financeから日足を取得。調整後株価は使わない。"""
    if not market.get("yahoo"):
        raise DataError("Yahooコードが設定されていません")
    history = yf.Ticker(market["yahoo"]).history(
        period="3mo", interval="1d", auto_adjust=False, actions=False, timeout=10
    )
    if history.empty or "Close" not in history:
        raise DataError("Yahoo Financeから日足データが返りませんでした")
    # UTCに変換すると日本の営業日が前日になり得るので、現地の日付を保持。
    dates = pd.DatetimeIndex(history.index)
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    return pd.DataFrame({"Date": dates, "Close": history["Close"].to_numpy()})


def parse_topix_html(html):
    """日本版YahooのTOPIX時系列表を、列名で探して読む。

    998405.Tは日本版Webの識別子。yfinance用シンボルではありません。
    ページ全体の数字やCSSクラス名から価格を推測しないようにします。
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    if not heading or "TOPIX" not in heading.get_text():
        raise DataError("TOPIXの時系列ページを確認できませんでした")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [cell.get_text(strip=True) for cell in rows[0].find_all(["th", "td"])]
        if "日付" not in headers or "終値" not in headers:
            continue
        date_column, close_column = headers.index("日付"), headers.index("終値")
        prices = []
        for row in rows[1:]:
            cells = [cell.get_text(strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) <= max(date_column, close_column):
                continue
            date_text = cells[date_column]
            if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", date_text):
                prices.append({"Date": date_text, "Close": cells[close_column].replace(",", "")})
        if prices:
            return pd.DataFrame(prices)
    raise DataError("TOPIXの時系列表がありません。提供元のページ形式が変わった可能性があります")


def read_topix():
    return parse_topix_html(get_text(TOPIX_URL))


def parse_stooq_csv(text):
    # HTTP 200でも、CSVではなく確認画面や「No data」の場合がある。
    if not text.lstrip("\ufeff\r\n ").startswith("Date,"):
        raise DataError("StooqがCSVを返しませんでした（データなし・接続制限など）")
    frame = pd.read_csv(StringIO(text))
    if not {"Date", "Close"}.issubset(frame.columns):
        raise DataError("StooqのCSV形式が変わっています")
    return frame


def read_stooq(market, today):
    if not market.get("stooq"):
        raise DataError("予備のStooqコードが設定されていません")
    text = get_text("https://stooq.com/q/d/l/", params={
        "s": market["stooq"], "i": "d",
        "d1": (today - timedelta(days=MAX_HISTORY_DAYS)).strftime("%Y%m%d"),
        "d2": today.strftime("%Y%m%d"),
    })
    return parse_stooq_csv(text)


def fetch_market(key, now=None, market=None):
    """Yahoo → Stooq。古いデータしかない場合も代替経路を試す。"""
    market = market or MARKETS[key]
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(market["zone"])).date()
    if market.get("kind") == "topix" or key == "topix":
        primary = ("Yahoo!ファイナンス（日本）", TOPIX_URL, read_topix)
    else:
        url = "https://finance.yahoo.com/quote/" + quote(market["yahoo"], safe="") + "/history/"
        primary = ("Yahoo Finance", url, lambda: read_yahoo(market))
    sources = [primary]
    if market.get("stooq"):
        sources.append(("Stooq", "https://stooq.com/q/?s=" + quote(market["stooq"], safe=""),
                        lambda: read_stooq(market, today)))
    candidates, notes = [], []
    for position, (source, url, reader) in enumerate(sources):
        try:
            data = clean_history(reader(), today)
            result = {"history": data, "source": source, "url": url,
                      "fetched_at": now.isoformat(), "fallback": position > 0}
            candidates.append(result)
            age = (today - data.iloc[-1]["Date"].date()).days
            if age <= OLD_AFTER_DAYS and len(data) >= 2:
                break
            notes.append(f"{source}: 日付が古い、または比較用の日足が不足しています")
        except Exception as error:
            # １つの取得元の障害で、他の指標やページ全体を止めない。
            detail = str(error) if isinstance(error, DataError) else type(error).__name__
            notes.append(f"{source}: {detail}")
    if not candidates:
        raise DataError(" / ".join(notes))
    # 代替元の値がもっと古い場合は、新しい方を選ぶ。系列は混ぜない。
    result = max(candidates, key=lambda item: (item["history"].iloc[-1]["Date"],
                                              len(item["history"]) >= 2))
    return {**result, "notes": notes}


@st.cache_data(ttl=CACHE_SECONDS, max_entries=50, show_spinner=False)
def cached_market(key, serialized_market=None):
    # 全取得元の失敗は例外にするので、成功したデータだけがキャッシュされる。
    market = json.loads(serialized_market) if serialized_market else MARKETS[key]
    return fetch_market(key, market=market)


def prefer_saved(fresh, saved):
    """代替元で日付が後退したときは、この画面で保持した値を残す。"""
    if saved and (fresh is None or saved["history"].iloc[-1]["Date"] >
                  fresh["history"].iloc[-1]["Date"]):
        return saved, True
    return fresh, False


def quote_values(result):
    data = result["history"]
    latest = float(data.iloc[-1]["Close"])
    if len(data) < 2:
        return latest, None, None
    previous = float(data.iloc[-2]["Close"])
    difference = latest - previous
    return latest, difference, difference / previous * 100


# ── 3. 表示 ────────────────────────────────────────────────
# 狭いSimple Browserでもカードを折り返す。数字と単位は別の行に表示する。
# Streamlitの内部CSSには依存せず、このアプリのカードだけを装飾する。
CARD_CSS = """
<style>
.market-grid {display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:16px;margin:20px 0 28px}
.market-card {container-type:inline-size;box-sizing:border-box;min-width:0;padding:22px 20px 18px;
    min-height:350px;border:1px solid #344153;border-radius:18px;background:#151e2c;color:#edf3fc;
    display:flex;flex-direction:column;box-shadow:0 8px 24px #00000012}
.market-card h3 {font:600 19px/1.4 sans-serif;margin:0;color:#edf3fc}
.market-caption {font-size:11px;letter-spacing:.08em;color:#9babbe;margin-top:5px;min-height:22px}
.market-value {font-size:clamp(23px,12cqi,35px);font-weight:700;line-height:1.3;
    font-variant-numeric:tabular-nums;letter-spacing:-.035em;margin-top:14px;overflow-wrap:anywhere}
.market-unit {font-size:12px;color:#aab9cd;margin:3px 0 10px}
.market-change {font-size:14px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.market-up {color:#6ee7b7}.market-down {color:#fda4af}.market-flat {color:#bdc9da}
.market-spark {width:100%;height:54px;display:block;margin:18px 0 10px}
.market-foot {font-size:12px;line-height:1.7;color:#aab9cd;margin-top:auto;overflow-wrap:anywhere}
.market-foot a {color:#a8caff;text-decoration:underline}
.market-jp {display:inline-block;margin-top:5px}
.market-status {font-size:12px;line-height:1.5;color:#94e6c8;margin:8px 0 0}
.market-warning {color:#f5cc80}.market-empty {font-size:25px;letter-spacing:0}
</style>
"""


def sparkline(data, change):
    values = data["Close"].tail(20).tolist()
    if len(values) < 2:
        return '<div class="market-spark"></div>'
    low, high = min(values), max(values)
    spread = high - low
    points = " ".join(f"{i * 200 / (len(values)-1):.1f},{48 - (v-low)/spread*42 if spread else 27:.1f}"
                      for i, v in enumerate(values))
    color = "#6ee7b7" if change is not None and change > 0 else "#fda4af" if change is not None and change < 0 else "#bdc9da"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 54">'
            f'<title>直近{len(values)}日足の推移（縦軸は指標ごとに異なります）</title>'
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>')
    # st.htmlはSVGタグを除去するため、自作SVGを画像として埋め込む。
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f'<img class="market-spark" alt="直近{len(values)}日足の推移" src="data:image/svg+xml;base64,{encoded}">'


def status_text(result, retained, today):
    if retained:
        return "更新できず、前回の値を表示", True
    if (today - result["history"].iloc[-1]["Date"].date()).days > OLD_AFTER_DAYS:
        return "データの日付が古い可能性があります", True
    if len(result["history"]) < 2:
        return "比較用の日足が不足しています", True
    return ("代替取得元を使用" if result["fallback"] else "取得済み"), False


def card_html(key, result, retained=False, market=None):
    market = market or MARKETS[key]
    top = (f'<article class="market-card"><h3>{escape(market["name"])}</h3>'
           f'<div class="market-caption">{escape(market["caption"])}</div>')
    jp_link = (f'<a class="market-jp" href="{escape(market["yahoo_jp"], quote=True)}" '
               'target="_blank" rel="noopener noreferrer">Yahoo!ファイナンス（日本）で詳細を見る ↗</a>')
    if result is None:
        return (top + '<div class="market-value market-empty">取得できません</div>'
                '<div class="market-unit">—</div><div class="market-change market-flat">比較データなし</div>'
                '<div class="market-spark"></div><div class="market-foot">時間をおいて「今すぐ更新」をお試しください。<br>'
                + jp_link + '</div>'
                '<div class="market-status market-warning">取得状況の詳細を下で確認できます</div></article>')
    data = result["history"]
    price, change, percent = quote_values(result)
    day = data.iloc[-1]["Date"].strftime("%Y/%m/%d")
    today = datetime.now(ZoneInfo(market["zone"])).date()
    status, warning = status_text(result, retained, today)
    direction = "up" if change is not None and change > 0 else "down" if change is not None and change < 0 else "flat"
    movement = f'{"↑" if change > 0 else "↓" if change < 0 else "→"} {change:+,.2f} ({percent:+.2f}%)' if change is not None else "前日比 —"
    compared = "比較日なし" if len(data) < 2 else "比較日 " + data.iloc[-2]["Date"].strftime("%m/%d")
    return (top + f'<div class="market-value">{price:,.2f}</div><div class="market-unit">{escape(market["unit"])}</div>'
            f'<div class="market-change market-{direction}">{movement}</div>' + sparkline(data, change) +
            f'<div class="market-foot">データ日 {day} · {compared}<br>'
            f'取得元：<a href="{escape(result["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{escape(result["source"])}</a><br>'
            + jp_link + '</div>'
            f'<div class="market-status {"market-warning" if warning else ""}">{status}</div></article>')


REGIONS = {
    "日本": "Asia/Tokyo",
    "米国・その他": "America/New_York",
    "為替": "Europe/London",
}


def persist_settings(items, notice):
    """設定を保存してから、安全に画面を作り直す。"""
    st.session_state["market_settings"] = save_market_settings(items)
    valid_ids = {item["id"] for item in items}
    st.session_state["last_success"] = {
        key: value for key, value in st.session_state.get("last_success", {}).items()
        if key in valid_ids
    }
    st.session_state["settings_notice"] = notice
    cached_market.clear()
    st.rerun()


def add_market(items, market, notice):
    """重複と上限を確認して1件追加する。"""
    if len(items) >= MAX_MARKETS:
        raise DataError(f"登録できる指標は{MAX_MARKETS}件までです")
    normalized = normalize_market(market)
    if any(item["id"] == normalized["id"] for item in items):
        raise DataError("同じ指標がすでに登録されています")
    if normalized["yahoo"] and any(item["yahoo"] == normalized["yahoo"] for item in items):
        raise DataError("同じYahooコードがすでに登録されています")
    persist_settings([*items, normalized], notice)


def sidebar_editor(items):
    """初心者向けの追加画面と、保存設定の管理画面。"""
    st.sidebar.header("⚙️ 表示する指標")
    st.sidebar.caption("ここでの変更はこのCodespacesに自動保存されます。")
    if notice := st.session_state.pop("settings_notice", None):
        st.sidebar.success(notice)

    with st.sidebar.expander("＋ 人気の指標から追加", expanded=True):
        available = [(key, market) for key, market in PRESET_MARKETS.items()
                     if key not in {item["id"] for item in items}]
        if available:
            labels = {market["name"]: (key, market) for key, market in available}
            selected = st.selectbox("追加する指標", list(labels), key="preset_choice")
            if st.button("この指標を追加", use_container_width=True):
                key, market = labels[selected]
                try:
                    add_market(items, {"id": key, **market}, f"{market['name']}を追加しました。")
                except DataError as error:
                    st.error(str(error))
        else:
            st.info("用意した人気指標はすべて登録済みです。")

    with st.sidebar.expander("＋ 好きなコードを入力"):
        st.caption("例：トヨタは 7203.T、Appleは AAPL、ビットコインは BTC-USD")
        with st.form("custom_market_form", clear_on_submit=False):
            name = st.text_input("表示名", placeholder="例：トヨタ")
            symbol = st.text_input("Yahooコード", placeholder="例：7203.T")
            unit = st.text_input("単位", value="円")
            caption = st.text_input("カードの小見出し（任意）", placeholder="例：TOYOTA")
            region = st.selectbox("市場・地域", list(REGIONS))
            stooq = st.text_input("予備のStooqコード（任意）",
                                  help="分からなければ空欄で大丈夫です。")
            submitted = st.form_submit_button("確認して追加", use_container_width=True)
        if submitted:
            symbol = symbol.strip().upper()
            custom = {"id": unique_market_id(symbol, items), "name": name, "yahoo": symbol,
                      "stooq": stooq, "unit": unit, "zone": REGIONS[region],
                      "caption": caption or symbol, "yahoo_jp": yahoo_japan_url(symbol),
                      "enabled": True}
            try:
                normalized = normalize_market(custom)
                if any(item["yahoo"] == normalized["yahoo"] for item in items):
                    raise DataError("同じYahooコードがすでに登録されています")
                with st.spinner("コードからデータを確認しています…"):
                    checked = fetch_market(normalized["id"], market=normalized)
                st.session_state.setdefault("last_success", {})[normalized["id"]] = checked
                add_market(items, normalized, f"{normalized['name']}を確認して追加しました。")
            except Exception as error:
                message = str(error) if isinstance(error, DataError) else type(error).__name__
                st.error("追加できませんでした。コードを確認してください。\n\n" + message)

    with st.sidebar.expander("表示順・表示／非表示・削除"):
        st.caption("▲▼で並べ替え、目のボタンで表示を切り替えます。")
        for index, market in enumerate(items):
            columns = st.columns([5, 1, 1, 1, 1])
            columns[0].write(("**" if market["enabled"] else "") + market["name"] +
                             ("**" if market["enabled"] else "（非表示）"))
            if columns[1].button("▲", key=f"up_{market['id']}", disabled=index == 0,
                                 help="上へ"):
                changed = deepcopy(items)
                changed[index - 1], changed[index] = changed[index], changed[index - 1]
                persist_settings(changed, "表示順を変更しました。")
            if columns[2].button("▼", key=f"down_{market['id']}", disabled=index == len(items) - 1,
                                 help="下へ"):
                changed = deepcopy(items)
                changed[index + 1], changed[index] = changed[index], changed[index + 1]
                persist_settings(changed, "表示順を変更しました。")
            eye = "🙈" if market["enabled"] else "👁"
            if columns[3].button(eye, key=f"toggle_{market['id']}",
                                 help="非表示にする" if market["enabled"] else "表示する"):
                changed = deepcopy(items)
                changed[index]["enabled"] = not changed[index]["enabled"]
                persist_settings(changed, "表示を変更しました。")
            if columns[4].button("✕", key=f"delete_{market['id']}", disabled=len(items) == 1,
                                 help="削除"):
                persist_settings([item for item in items if item["id"] != market["id"]],
                                 f"{market['name']}を削除しました。")

    with st.sidebar.expander("表示名・単位を編集"):
        selected_id = st.selectbox("編集する指標", [item["id"] for item in items],
                                   format_func=lambda value: next(
                                       item["name"] for item in items if item["id"] == value),
                                   key="edit_choice")
        selected_index = next(i for i, item in enumerate(items) if item["id"] == selected_id)
        selected_market = items[selected_index]
        with st.form("edit_market_form"):
            edited_name = st.text_input("表示名", value=selected_market["name"])
            edited_unit = st.text_input("単位", value=selected_market["unit"])
            edited_caption = st.text_input("カードの小見出し", value=selected_market["caption"])
            edit_submitted = st.form_submit_button("変更を保存", use_container_width=True)
        if edit_submitted:
            changed = deepcopy(items)
            changed[selected_index].update(name=edited_name, unit=edited_unit,
                                           caption=edited_caption)
            try:
                persist_settings(changed, f"{edited_name}の表示を変更しました。")
            except DataError as error:
                st.error(str(error))

    with st.sidebar.expander("設定のバックアップ・復元"):
        settings_json = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
        st.download_button("設定をダウンロード", settings_json,
                           file_name="dashboard_markets.json", mime="application/json",
                           use_container_width=True)
        uploaded = st.file_uploader("保存した設定を読み込む", type=["json"])
        if st.button("読み込んだ設定に置き換える", disabled=uploaded is None,
                     use_container_width=True):
            try:
                restored = validate_market_list(json.loads(uploaded.getvalue().decode("utf-8")))
                persist_settings(restored, "バックアップから設定を復元しました。")
            except (UnicodeDecodeError, json.JSONDecodeError, DataError) as error:
                st.error("設定ファイルを読み込めませんでした：" + str(error))

        if st.button("初期の5指標に戻す", use_container_width=True):
            st.session_state["confirm_reset"] = True
        if st.session_state.get("confirm_reset"):
            st.warning("追加・編集した設定を初期状態へ戻します。")
            yes, no = st.columns(2)
            if yes.button("戻す", type="primary", use_container_width=True):
                st.session_state["confirm_reset"] = False
                persist_settings(default_market_list(), "初期の5指標に戻しました。")
            if no.button("キャンセル", use_container_width=True):
                st.session_state["confirm_reset"] = False
                st.rerun()


def main():
    st.set_page_config(page_title="My Investment Dashboard", page_icon="📈", layout="wide")
    if "market_settings" not in st.session_state:
        loaded, load_error = load_market_settings()
        st.session_state["market_settings"] = loaded
        if load_error:
            st.session_state["settings_load_error"] = load_error
    items = st.session_state["market_settings"]
    sidebar_editor(items)

    st.title("📈 My Investment Dashboard")
    st.caption("日本と米国の主要指数・ドル円を、ひとつの画面で。")
    st.write("日足ベースの参考値です。市場・取得元によりデータ日は異なり、当日の日足は取引中に変動します。")
    st.caption("前日比は同じ取得元の直近２日足で計算。５分間は取得結果を再利用します（自動更新ではありません）。")
    if load_error := st.session_state.pop("settings_load_error", None):
        st.warning(load_error)

    markets = {market["id"]: market for market in items if market["enabled"]}
    if not markets:
        st.info("表示中の指標がありません。左のサイドバーで目のボタンを押して表示してください。")
        return

    if st.button("↻ 今すぐ更新", type="primary"):
        cached_market.clear()  # 前回成功した値は消さず、取得キャッシュだけを更新

    saved = st.session_state.setdefault("last_success", {})
    results, retained, diagnostics = {}, {}, {}
    progress = st.empty()
    for key, market in markets.items():
        progress.caption(f'{market["name"]}を確認しています…')
        fresh = None
        try:
            fresh = cached_market(key, market_payload(market))
            diagnostics[key] = fresh["notes"]
        except Exception as error:
            diagnostics[key] = [str(error) if isinstance(error, DataError) else type(error).__name__]
        result, is_retained = prefer_saved(fresh, saved.get(key))
        if is_retained:
            diagnostics[key].append("更新失敗、または新しい取得結果の日付が前回より古いため、前回の値を保持しました")
        if result is not None:
            saved[key] = result
        results[key], retained[key] = result, is_retained
    progress.empty()

    st.html(CARD_CSS + '<section class="market-grid" aria-label="主要指標">' +
            "".join(card_html(key, results[key], retained[key], market)
                    for key, market in markets.items()) + '</section>')
    failed = [markets[key]["name"] for key in markets if results[key] is None]
    if failed:
        st.warning("取得できなかった指標：" + "、".join(failed) + "。他の指標は引き続き表示しています。")

    st.subheader("一覧")
    rows = []
    for key, market in markets.items():
        result = results[key]
        if result is None:
            rows.append({"指標": market["name"], "日足参考値": "—", "前日差": "—", "前日比": "—",
                         "データ日": "—", "比較日": "—", "取得元": "—", "状態": "取得失敗"})
            continue
        data = result["history"]
        price, change, percent = quote_values(result)
        status, _ = status_text(result, retained[key], datetime.now(ZoneInfo(market["zone"])).date())
        rows.append({"指標": market["name"], "日足参考値": f'{price:,.2f} {market["unit"]}',
                     "前日差": f"{change:+,.2f}" if change is not None else "—",
                     "前日比": f"{percent:+.2f}%" if percent is not None else "—",
                     "データ日": data.iloc[-1]["Date"].strftime("%Y/%m/%d"),
                     "比較日": data.iloc[-2]["Date"].strftime("%Y/%m/%d") if len(data) >= 2 else "—",
                     "取得元": result["source"], "状態": status})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    with st.expander("取得状況・データの読み方"):
        st.write("TOPIXは日本版Yahoo!の時系列表、その他はYahoo Financeから取得します。予備コードがある指標は、失敗時にStooqも試します。")
        st.write("取得元が変わっても、前日比は同じ取得元の２日足を使います。比較日が離れている場合は、その期間の変化です。")
        st.write("ミニチャートは最大20日足で、縦軸の縮尺は指標ごとに異なります。休日や更新時刻により、データ日はそろいません。")
        st.write("指標の登録内容はdashboard_markets.jsonに保存します。価格の『前回の値』はこの画面のセッション内だけで保持します。")
        for key, market in markets.items():
            st.markdown(f'**{market["name"]}**')
            if results[key]:
                fetched = datetime.fromisoformat(results[key]["fetched_at"]).astimezone(ZoneInfo("Asia/Tokyo"))
                st.caption(f'表示データの取得日時：{fetched:%Y/%m/%d %H:%M:%S} JST ／ {results[key]["source"]}')
            if diagnostics[key]:
                for note in diagnostics[key]:
                    st.text(note)
            else:
                st.caption("取得できました。")

    # Sector Radarは既存の指標カードから独立しているため、障害が起きても
    # 上のダッシュボードはそのまま使えます。
    try:
        from sector_radar import render_sector_radar

        render_sector_radar()
    except Exception as error:
        st.warning("Sector Radarを表示できませんでした。時間を置いて再度お試しください。")
        with st.expander("Sector Radarのエラー情報"):
            st.text(f"{type(error).__name__}: {error}")


if __name__ == "__main__":
    main()
