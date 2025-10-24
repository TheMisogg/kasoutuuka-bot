"""
flow_filters.py — helpers + HTTP fetchers (compat).
Provides:
  - fetch_recent_trades_linear(symbol, limit=1000)
  - fetch_orderbook_linear(symbol, depth=50)
  - compute_flow_metrics(trades, window_sec)
  - compute_wall_pressure(book, depth=50)
"""
from __future__ import annotations
from typing import List, Dict, Any, Tuple
import time, requests

try:
    from config import API
    BASE = getattr(API, "base_url", "https://api.bybit.com")
except Exception:
    BASE = "https://api.bybit.com"

# ------------------ HTTP fetchers (v5 public) ------------------

def fetch_recent_trades_linear(symbol: str = "SOLUSDT", limit: int = 1000) -> List[Dict[str, Any]]:
    """
    GET /v5/market/recent-trade  (category=linear)
    Normalizes fields to:
      {"time": <ms>, "price": <float>, "size": <float>, "side": "Buy"/"Sell"}
    """
    url = BASE + "/v5/market/recent-trade"
    params = {"category": "linear", "symbol": symbol, "limit": int(limit)}
    try:
        r = requests.get(url, params=params, timeout=4)
        j = r.json()
        if j.get("retCode") != 0:
            return []
        data = j.get("result", {}).get("list", []) or []
        out = []
        for d in data:
            # keys: time, price, size, side (Bybit v5 spec)
            try:
                out.append({
                    "time": int(d.get("time") or d.get("ts") or 0),
                    "price": float(d.get("price") or d.get("p") or 0.0),
                    "size": float(d.get("size") or d.get("q") or 0.0),
                    "side": d.get("side") or d.get("S") or "",
                })
            except Exception:
                continue
        # API returns desc order already; keep as-is
        return out
    except Exception:
        return []

def fetch_orderbook_linear(symbol: str = "SOLUSDT", depth: int = 50) -> Dict[str, Any]:
    """
    GET /v5/market/orderbook  (category=linear)
    Returns dict with 'a' (asks) and 'b' (bids) arrays:
      a/b: list of [price, size] strings from API (we keep original to avoid float drift)
    """
    url = BASE + "/v5/market/orderbook"
    params = {"category": "linear", "symbol": symbol, "limit": int(depth)}
    try:
        r = requests.get(url, params=params, timeout=4)
        j = r.json()
        if j.get("retCode") != 0:
            return {}
        res = j.get("result", {}) or {}
        return {"a": res.get("a", []), "b": res.get("b", [])}
    except Exception:
        return {}

# ------------------ Local computations ------------------

def _within_window(trades: List[Dict[str, Any]], window_sec: int) -> List[Dict[str, Any]]:
    w = int(window_sec) * 1000
    if trades:
        try:
            now_ms = int(trades[0].get("time") or trades[-1].get("time") or 0)
        except Exception:
            now_ms = int(time.time() * 1000)
    else:
        now_ms = int(time.time() * 1000)
    out = []
    for t in trades:
        try:
            if (now_ms - int(t.get("time", 0))) <= w:
                out.append(t)
        except Exception:
            continue
    return out

def _usd_value(t: Dict[str, Any]) -> float:
    px = float(t.get("price") or t.get("p") or 0.0)
    qty = float(t.get("size") or t.get("q") or 0.0)
    return px * qty

def compute_flow_metrics(trades: List[Dict[str, Any]], window_sec: int) -> Dict[str, Any]:
    win = _within_window(trades, window_sec)
    cnt = len(win)
    if cnt == 0:
        return {"count": 0, "consec": 0, "imbalance": 0.0, "rate_usd": 0.0, "net_usd": 0.0}
    ms_span = max(1, (int(win[0]["time"]) - int(win[-1]["time"])))
    sec_span = max(1.0, ms_span / 1000.0)
    net = 0.0
    consec = 0
    last_side = None
    imb_n = 0
    for t in win:
        side = (t.get("side") or "").lower()
        usd = _usd_value(t)
        if side == "buy":
            net += usd
            if last_side in (None, "buy"): consec += 1
            last_side = "buy"; imb_n += 1
        elif side == "sell":
            net -= usd
            if last_side in (None, "sell"): consec += 1
            last_side = "sell"; imb_n -= 1
    imbalance = max(-1.0, min(1.0, imb_n / float(cnt))) if cnt else 0.0
    rate = net / sec_span
    return {"count": cnt, "consec": consec, "imbalance": imbalance, "rate_usd": rate, "net_usd": net}

def compute_wall_pressure(book: Dict[str, Any], depth: int = 50) -> Tuple[float, float, float]:
    if not book:
        return (0.0, 0.0, 0.0)
    asks = book.get("a") or book.get("asks") or []
    bids = book.get("b") or book.get("bids") or []
    try:
        asks = [(float(p), float(q)) for p, q in (asks[:depth])]
        bids = [(float(p), float(q)) for p, q in (bids[:depth])]
    except Exception:
        asks = [(float(x.get("price", 0)), float(x.get("size", 0))) for x in (asks[:depth])]
        bids = [(float(x.get("price", 0)), float(x.get("size", 0))) for x in (bids[:depth])]
    ask_sum = sum(p*q for p, q in asks)
    bid_sum = sum(p*q for p, q in bids)
    ratio = (ask_sum / bid_sum) if bid_sum > 0 else 0.0
    return (ratio, ask_sum, bid_sum)
