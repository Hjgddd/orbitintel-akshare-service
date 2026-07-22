�r�^�f��ئ{]ly�'vî���"""OrbitIntel server-side AKShare adapter.

AKShare is a Python library, while the public OrbitIntel site runs on a
Cloudflare-compatible TypeScript Worker. This service keeps Python and the
edge app separate and exposes normalized JSON contracts to the site.
"""

from __future__ import annotations

import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import akshare as ak
import pandas as pd
from fastapi import FastAPI, Header, HTTPException


app = FastAPI(title="OrbitIntel AKShare Adapter", version="1.0.0")
SERVICE_KEY = os.getenv("AKSHARE_SERVICE_KEY", "").strip()
_CACHE: Dict[str, Tuple[float, Any]] = {}

INDEX_SPECS = {
    "000001": {"name": "\u4e0a\u8bc1\u6307\u6570", "symbol": "sh000001"},
    "399001": {"name": "\u6df1\u8bc1\u6210\u6307", "symbol": "sz399001"},
    "399006": {"name": "\u521b\u4e1a\u677f\u6307", "symbol": "sz399006"},
}

KNOWN_SECURITY_NAMES = {
    "000001": "\u4e0a\u8bc1\u6307\u6570",
    "300750": "\u5b81\u5fb7\u65f6\u4ee3",
    "600519": "\u8d35\u5dde\u8305\u53f0",
    "688981": "\u4e2d\u82af\u56fd\u9645",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require_key(value: Optional[str]) -> None:
    if SERVICE_KEY and value != SERVICE_KEY:
        raise HTTPException(status_code=401, detail="invalid AKShare adapter key")


def cached(key: str, ttl_seconds: int, loader: Callable[[], Any]) -> Any:
    current = time.time()
    hit = _CACHE.get(key)
    if hit and current - hit[0] < ttl_seconds:
        return hit[1]
    value = loader()
    _CACHE[key] = (current, value)
    return value


def clean_code(value: str) -> str:
    match = re.search(r"(\d{6})", str(value))
    if not match:
        raise HTTPException(status_code=400, detail="code must contain six digits")
    return match.group(1)


def market_for(code: str) -> str:
    if code.startswith("6"):
        return "sh"
    if code.startswith("8") or code.startswith("4"):
        return "bj"
    return "sz"


def safe_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        return safe_value(value.item())
    if isinstance(value, dict):
        return {str(key): safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(item) for item in value]
    return value


def records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return [safe_value(row) for row in frame.to_dict(orient="records")]


def pick(row: Dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def history_rows(symbol: str, period: str = "daily", lookback_days: int = 730) -> List[Dict[str, Any]]:
    today = datetime.now().date()
    start = (today - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end = (today + timedelta(days=1)).strftime("%Y%m%d")
    frame = cached(
        f"hist-{symbol}-{period}",
        60,
        lambda: ak.stock_zh_a_hist(symbol=symbol, period=period, start_date=start, end_date=end, adjust="qfq"),
    )
    return records(frame)


def history_quote(code: str) -> Dict[str, Any]:
    rows = history_rows(code)
    if not rows:
        raise HTTPException(status_code=503, detail=f"no reliable historical quote for {code}")
    row = rows[-1]
    previous = rows[-2] if len(rows) > 1 else row
    price = number(pick(row, "\u6536\u76d8", "close"))
    previous_close = number(pick(previous, "\u6536\u76d8", "close"), price)
    change = price - previous_close
    change_pct = change / previous_close * 100 if previous_close else 0.0
    as_of = str(pick(row, "\u65e5\u671f", "date", default=""))
    return {
        "code": code,
        "name": KNOWN_SECURITY_NAMES.get(code, code),
        "price": price,
        "change": change,
        "changePct": change_pct,
        "open": number(pick(row, "\u5f00\u76d8", "open")),
        "high": number(pick(row, "\u6700\u9ad8", "high")),
        "low": number(pick(row, "\u6700\u4f4e", "low")),
        "previousClose": previous_close,
        "volume": number(pick(row, "\u6210\u4ea4\u91cf", "volume")),
        "amount": number(pick(row, "\u6210\u4ea4\u989d", "amount")),
        "turnover": 0.0,
        "marketCap": 0.0,
        "timestamp": now_iso(),
        "exchangeTimestamp": as_of,
        "source": "AKShare / stock_zh_a_hist",
        "dataLevel": "daily-close",
        "isDelayed": True,
        "isEstimated": False,
        "isDemo": False,
        "notice": "\u514d\u8d39\u6570\u636e\u6e90\u6682\u672a\u63d0\u4f9b\u53ef\u9760\u5b9e\u65f6\u5feb\u7167\uff0c\u5f53\u524d\u663e\u793a\u6700\u8fd1\u4ea4\u6613\u65e5\u6536\u76d8\u6570\u636e\u3002",
    }


def quote_row(code: str) -> Dict[str, Any]:
    try:
        frame = cached("a-spot", 8, lambda: ak.stock_zh_a_spot_em())
        rows = records(frame)
        row = next((item for item in rows if str(pick(item, "\u4ee3\u7801", "code", default="")).zfill(6) == code), None)
        if row:
            return {
                "code": code,
                "name": str(pick(row, "\u540d\u79f0", "name", default=code)),
                "price": number(pick(row, "\u6700\u65b0\u4ef7", "price")),
                "change": number(pick(row, "\u6da8\u8dcc\u989d", "change")),
                "changePct": number(pick(row, "\u6da8\u8dcc\u5e45", "changePct")),
                "open": number(pick(row, "\u4eca\u5f00", "open")),
                "high": number(pick(row, "\u6700\u9ad8", "high")),
                "low": number(pick(row, "\u6700\u4f4e", "low")),
                "previousClose": number(pick(row, "\u6628\u6536", "previousClose")),
                "volume": number(pick(row, "\u6210\u4ea4\u91cf", "volume")),
                "amount": number(pick(row, "\u6210\u4ea4\u989d", "amount")),
                "turnover": number(pick(row, "\u6362\u624b\u7387", "turnover")),
                "marketCap": number(pick(row, "\u603b\u5e02\u503c", "marketCap")),
                "timestamp": now_iso(),
                "source": "AKShare / stock_zh_a_spot_em",
                "dataLevel": "snapshot",
                "isDelayed": False,
                "isEstimated": False,
                "isDemo": False,
            }
    except Exception:
        pass
    return history_quote(code)


def index_rows() -> List[Dict[str, Any]]:
    try:
        frame = cached("important-index-spot", 12, lambda: ak.stock_zh_index_spot_em(symbol="\u6caa\u6df1\u91cd\u8981\u6307\u6570"))
        rows = records(frame)
        if rows:
            return rows
    except Exception:
        pass

    fallback: List[Dict[str, Any]] = []
    for code, spec in INDEX_SPECS.items():
        try:
            frame = cached(f"index-hist-{code}", 60, lambda spec=spec: ak.stock_zh_index_daily_em(symbol=spec["symbol"]))
            rows = records(frame)
            if not rows:
                continue
            row = rows[-1]
            previous = rows[-2] if len(rows) > 1 else row
            value = number(pick(row, "\u6536\u76d8", "close"))
            previous_value = number(pick(previous, "\u6536\u76d8", "close"), value)
            change_pct = (value - previous_value) / previous_value * 100 if previous_value else 0.0
            fallback.append({
                "\u4ee3\u7801": code,
                "\u540d\u79f0": spec["name"],
                "\u6700\u65b0\u4ef7": value,
                "\u6da8\u8dcc\u5e45": change_pct,
                "\u65e5\u671f": pick(row, "\u65e5\u671f", "date", default=""),
            })
        except Exception:
            continue
    return fallback


@app.get("/health")
def health(x_akshare_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_key(x_akshare_key)
    return {"status": "ok", "provider": "AKShare", "version": getattr(ak, "__version__", "unknown"), "checkedAt": now_iso()}


@app.get("/v1/quote")
def quote(code: str, x_akshare_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_key(x_akshare_key)
    return {"data": quote_row(clean_code(code))}


@app.get("/v1/market")
def market(x_akshare_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_key(x_akshare_key)
    wanted = {"000001": "\u4e0a\u8bc1\u6307\u6570", "399001": "\u6df1\u8bc1\u6210\u6307", "399006": "\u521b\u4e1a\u677f\u6307"}
    rows = []
    breadth_status = "unavailable"
    up = down = flat = 0
    try:
        breadth = cached("a-spot", 8, lambda: records(ak.stock_zh_a_spot_em()))
        up = sum(1 for item in breadth if number(pick(item, "\u6da8\u8dcc\u5e45", "changePct")) > 0)
        down = sum(1 for item in breadth if number(pick(item, "\u6da8\u8dcc\u5e45", "changePct")) < 0)
        flat = max(len(breadth) - up - down, 0)
        breadth_status = "available"
    except Exception:
        pass
    for row in index_rows():
        code = str(pick(row, "\u4ee3\u7801", "code", default="")).zfill(6)
        if code not in wanted:
            continue
        rows.append({
            "code": code,
            "name": str(pick(row, "\u540d\u79f0", "name", default=wanted[code])),
            "value": number(pick(row, "\u6700\u65b0\u4ef7", "value", "price")),
            "change": number(pick(row, "\u6da8\u8dcc\u5e45", "changePct")),
            "up": up,
            "down": down,
            "flat": flat,
        })
    if not rows:
        raise HTTPException(status_code=502, detail="AKShare returned no important index quotes")
    return {
        "data": {
            "quotes": rows,
            "timestamp": now_iso(),
            "breadthStatus": breadth_status,
            "breadthNotice": "\u514d\u8d39\u6e90\u672a\u8fd4\u56de\u53ef\u9760\u7684\u5168\u5e02\u573a\u5feb\u7167\uff0c\u6da8\u8dcc\u5bb6\u6570\u4e0d\u5c55\u793a\u4f30\u7b97\u503c\u3002" if breadth_status != "available" else None,
            "source": "AKShare",
            "dataLevel": "daily-close-fallback" if breadth_status != "available" else "snapshot",
        }
    }


@app.get("/v1/bars")
def bars(code: str, period: str = "1d", x_akshare_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_key(x_akshare_key)
    symbol = clean_code(code)
    today = datetime.now().date()
    if period in {"1d", "1w", "1mo"}:
        period_name = {"1d": "daily", "1w": "weekly", "1mo": "monthly"}[period]
        start = (today - timedelta(days=730 if period == "1d" else 3650)).strftime("%Y%m%d")
        end = (today + timedelta(days=1)).strftime("%Y%m%d")
        frame = cached(f"hist-{symbol}-{period}", 60, lambda: ak.stock_zh_a_hist(symbol=symbol, period=period_name, start_date=start, end_date=end, adjust="qfq"))
        raw = records(frame)
        normalized = [{
            "time": str(pick(row, "\u65e5\u671f", "date", default="")),
            "open": number(pick(row, "\u5f00\u76d8", "open")),
            "high": number(pick(row, "\u6700\u9ad8", "high")),
            "low": number(pick(row, "\u6700\u4f4e", "low")),
            "close": number(pick(row, "\u6536\u76d8", "close")),
            "volume": number(pick(row, "\u6210\u4ea4\u91cf", "volume")),
            "amount": number(pick(row, "\u6210\u4ea4\u989d", "amount")),
        } for row in raw]
    else:
        minute_period = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}.get(period, "5")
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        frame = cached(f"minute-{symbol}-{minute_period}", 20, lambda: ak.stock_zh_a_hist_min_em(symbol=symbol, start_date=start, end_date=end, period=minute_period, adjust=""))
        raw = records(frame)
        normalized = [{
            "time": str(pick(row, "\u65f6\u95f4", "day", "timestamp", default="")),
            "open": number(pick(row, "\u5f00\u76d8", "open")),
            "high": number(pick(row, "\u6700\u9ad8", "high")),
            "low": number(pick(row, "\u6700\u4f4e", "low")),
            "close": number(pick(row, "\u6536\u76d8", "close")),
            "volume": number(pick(row, "\u6210\u4ea4\u91cf", "volume")),
            "amount": number(pick(row, "\u6210\u4ea4\u989d", "amount")),
        } for row in raw]
    normalized = [row for row in normalized if row["time"] and row["close"] > 0]
    if not normalized:
        raise HTTPException(status_code=404, detail=f"no bars for {symbol}")
    return {"data": {"bars": normalized, "timestamp": now_iso()}}


@app.get("/v1/orderbook")
def orderbook(code: str, x_akshare_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_key(x_akshare_key)
    symbol = clean_code(code)
    frame = ak.stock_bid_ask_em(symbol=symbol)
    raw = records(frame)
    levels: Dict[str, float] = {}
    for row in raw:
        item = str(pick(row, "item", "\u540d\u79f0", default=""))
        levels[item] = number(pick(row, "value", "\u503c", default=0))
    bids = [{"price": levels.get(f"buy_{level}", 0), "volume": levels.get(f"buy_{level}_vol", 0)} for level in range(1, 6)]
    asks = [{"price": levels.get(f"sell_{level}", 0), "volume": levels.get(f"sell_{level}_vol", 0)} for level in range(1, 6)]
    bids = [row for row in bids if row["price"] > 0]
    asks = [row for row in asks if row["price"] > 0]
    if not bids and not asks:
        raise HTTPException(status_code=502, detail="AKShare returned no five-level orderbook")
    return {"data": {"code": symbol, "bids": bids, "asks": asks, "updatedAt": now_iso()}}


@app.get("/v1/flow")
def flow(code: str, x_akshare_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_key(x_akshare_key)
    symbol = clean_code(code)
    frame = cached(f"flow-{symbol}", 60, lambda: ak.stock_individual_fund_flow(stock=symbol, market=market_for(symbol)))
    raw = records(frame)
    if not raw:
        raise HTTPException(status_code=404, detail=f"no money flow for {symbol}")
    row = raw[-1]
    main = number(pick(row, "\u4e3b\u529b\u51c0\u6d41\u5165-\u51c0\u989d", "\u4e3b\u529b\u51c0\u6d41\u5165", "netAmount"))
    large = number(pick(row, "\u5927\u5355\u51c0\u6d41\u5165-\u51c0\u989d", "largeNet"))
    medium = number(pick(row, "\u4e2d\u5355\u51c0\u6d41\u5165-\u51c0\u989d", "mediumNet"))
    small = number(pick(row, "\u5c0f\u5355\u51c0\u6d41\u5165-\u51c0\u989d", "smallNet"))
    return {"data": {"flow": {
        "code": symbol,
        "tradeDate": str(pick(row, "\u65e5\u671f", "tradeDate", default="")),
        "netAmount": main,
        "largeBuyAmount": max(large, 0),
        "largeSellAmount": max(-large, 0),
        "mediumBuyAmount": max(medium, 0),
        "mediumSellAmount": max(-medium, 0),
        "smallBuyAmount": max(small, 0),
        "smallSellAmount": max(-small, 0),
    }, "timestamp": now_iso()}}


@app.get("/v1/news")
def news(code: Optional[str] = None, x_akshare_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_key(x_akshare_key)
    symbol = clean_code(code) if code else "\u5e02\u573a"
    if code:
        frame = cached(f"news-{symbol}", 60, lambda: ak.stock_news_em(symbol=symbol))
        raw = records(frame)
        normalized = [{
            "id": f"akshare-{symbol}-{index}",
            "title": str(pick(row, "\u65b0\u95fb\u6807\u9898", "title", default="")),
            "content": str(pick(row, "\u65b0\u95fb\u5185\u5bb9", "content", default="")),
            "source": str(pick(row, "\u6587\u7ae0\u6765\u6e90", "source", default="\u4e1c\u65b9\u8d22\u5bcc")),
            "publishedAt": str(pick(row, "\u53d1\u5e03\u65f6\u95f4", "publishedAt", default=now_iso())),
            "url": str(pick(row, "\u65b0\u95fb\u94fe\u63a5", "url", default="")),
        } for index, row in enumerate(raw)]
    else:
        frame = cached("market-news", 60, lambda: ak.stock_news_main_cx())
        raw = records(frame)
        normalized = [{
            "id": f"akshare-market-{index}",
            "title": str(pick(row, "summary", "\u6807\u9898", "title", default="")),
            "content": str(pick(row, "summary", "\u5185\u5bb9", "content", default="")),
            "source": "\u8d22\u65b0\u6570\u636e\u901a",
            "publishedAt": now_iso(),
            "url": str(pick(row, "url", "\u94fe\u63a5", default="")),
        } for index, row in enumerate(raw)]
    normalized = [row for row in normalized if row["title"]]
    return {"data": {"news": normalized[:100], "timestamp": now_iso()}}
