"""OrbitIntel server-side AKShare adapter.

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


def quote_row(code: str) -> Dict[str, Any]:
    frame = cached("a-spot", 8, lambda: ak.stock_zh_a_spot_em())
    rows = records(frame)
    row = next((item for item in rows if str(pick(item, "代码", "code", default="")).zfill(6) == code), None)
    if not row:
        raise HTTPException(status_code=404, detail=f"quote not found for {code}")
    return {
        "code": code,
        "name": str(pick(row, "名称", "name", default=code)),
        "price": number(pick(row, "最新价", "price")),
        "change": number(pick(row, "涨跌额", "change")),
        "changePct": number(pick(row, "涨跌幅", "changePct")),
        "open": number(pick(row, "今开", "open")),
        "high": number(pick(row, "最高", "high")),
        "low": number(pick(row, "最低", "low")),
        "previousClose": number(pick(row, "昨收", "previousClose")),
        "volume": number(pick(row, "成交量", "volume")),
        "amount": number(pick(row, "成交额", "amount")),
        "turnover": number(pick(row, "换手率", "turnover")),
        "marketCap": number(pick(row, "总市值", "marketCap")),
        "timestamp": now_iso(),
    }


def index_rows() -> List[Dict[str, Any]]:
    frame = cached("important-index-spot", 12, lambda: ak.stock_zh_index_spot_em(symbol="沪深重要指数"))
    return records(frame)


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
    wanted = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指"}
    rows = []
    breadth = cached("a-spot", 8, lambda: records(ak.stock_zh_a_spot_em()))
    up = sum(1 for item in breadth if number(pick(item, "涨跌幅", "changePct")) > 0)
    down = sum(1 for item in breadth if number(pick(item, "涨跌幅", "changePct")) < 0)
    flat = max(len(breadth) - up - down, 0)
    for row in index_rows():
        code = str(pick(row, "代码", "code", default="")).zfill(6)
        if code not in wanted:
            continue
        rows.append({
            "code": code,
            "name": str(pick(row, "名称", "name", default=wanted[code])),
            "value": number(pick(row, "最新价", "value", "price")),
            "change": number(pick(row, "涨跌幅", "changePct")),
            "up": up,
            "down": down,
            "flat": flat,
        })
    if not rows:
        raise HTTPException(status_code=502, detail="AKShare returned no important index quotes")
    return {"data": {"quotes": rows, "timestamp": now_iso()}}


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
            "time": str(pick(row, "日期", "date", default="")),
            "open": number(pick(row, "开盘", "open")),
            "high": number(pick(row, "最高", "high")),
            "low": number(pick(row, "最低", "low")),
            "close": number(pick(row, "收盘", "close")),
            "volume": number(pick(row, "成交量", "volume")),
            "amount": number(pick(row, "成交额", "amount")),
        } for row in raw]
    else:
        minute_period = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}.get(period, "5")
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        frame = cached(f"minute-{symbol}-{minute_period}", 20, lambda: ak.stock_zh_a_hist_min_em(symbol=symbol, start_date=start, end_date=end, period=minute_period, adjust=""))
        raw = records(frame)
        normalized = [{
            "time": str(pick(row, "时间", "day", "timestamp", default="")),
            "open": number(pick(row, "开盘", "open")),
            "high": number(pick(row, "最高", "high")),
            "low": number(pick(row, "最低", "low")),
            "close": number(pick(row, "收盘", "close")),
            "volume": number(pick(row, "成交量", "volume")),
            "amount": number(pick(row, "成交额", "amount")),
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
        item = str(pick(row, "item", "名称", default=""))
        levels[item] = number(pick(row, "value", "值", default=0))
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
    main = number(pick(row, "主力净流入-净额", "主力净流入", "netAmount"))
    large = number(pick(row, "大单净流入-净额", "largeNet"))
    medium = number(pick(row, "中单净流入-净额", "mediumNet"))
    small = number(pick(row, "小单净流入-净额", "smallNet"))
    return {"data": {"flow": {
        "code": symbol,
        "tradeDate": str(pick(row, "日期", "tradeDate", default="")),
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
    symbol = clean_code(code) if code else "市场"
    if code:
        frame = cached(f"news-{symbol}", 60, lambda: ak.stock_news_em(symbol=symbol))
        raw = records(frame)
        normalized = [{
            "id": f"akshare-{symbol}-{index}",
            "title": str(pick(row, "新闻标题", "title", default="")),
            "content": str(pick(row, "新闻内容", "content", default="")),
            "source": str(pick(row, "文章来源", "source", default="东方财富")),
            "publishedAt": str(pick(row, "发布时间", "publishedAt", default=now_iso())),
            "url": str(pick(row, "新闻链接", "url", default="")),
        } for index, row in enumerate(raw)]
    else:
        frame = cached("market-news", 60, lambda: ak.stock_news_main_cx())
        raw = records(frame)
        normalized = [{
            "id": f"akshare-market-{index}",
            "title": str(pick(row, "summary", "标题", "title", default="")),
            "content": str(pick(row, "summary", "内容", "content", default="")),
            "source": "财新数据通",
            "publishedAt": now_iso(),
            "url": str(pick(row, "url", "链接", default="")),
        } for index, row in enumerate(raw)]
    normalized = [row for row in normalized if row["title"]]
    return {"data": {"news": normalized[:100], "timestamp": now_iso()}}
