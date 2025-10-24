# bybit.py — v5署名方式・Unified対応（発注10001対策：reduceOnlyをbool、qtyをステップ丸め）
# 元版に最小限の変更のみ行っています（残高取得ロジックはそのまま）。
from __future__ import annotations

import os, time, hmac, hashlib, json, urllib.request
from typing import Dict, Any, List, Tuple
from urllib.parse import urlencode
from .config import API

def _ts_ms() -> int:
    return int(time.time() * 1000)

def _api_key() -> str:
    k = os.getenv("BYBIT_API_KEY")
    if not k: raise ValueError("BYBIT_API_KEY 未設定")
    return k

def _api_secret() -> str:
    s = os.getenv("BYBIT_API_SECRET")
    if not s: raise ValueError("BYBIT_API_SECRET 未設定")
    return s

def _headers_base() -> Dict[str, str]:
    return {"Content-Type": "application/json", "X-BAPI-API-KEY": _api_key()}

def _sign_v5(ts: int, recv_window: int, payload: str) -> str:
    presign = f"{ts}{_api_key()}{recv_window}{payload}"
    return hmac.new(_api_secret().encode(), presign.encode(), hashlib.sha256).hexdigest()

def _public_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{API.base_url}{path}?{urlencode(params)}"
    req = urllib.request.Request(url, headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "BybitBot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

def _private_get(path: str, biz_params: Dict[str, Any]) -> Dict[str, Any]:
    ts = _ts_ms()
    recv_window = 5000
    query = "&".join(f"{k}={biz_params[k]}" for k in sorted(biz_params))
    sign = _sign_v5(ts, recv_window, query)
    headers = _headers_base()
    headers["X-BAPI-TIMESTAMP"] = str(ts)
    headers["X-BAPI-RECV-WINDOW"] = str(recv_window)
    headers["X-BAPI-SIGN"] = sign
    url = f"{API.base_url}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

def _private_post(path: str, biz_body: Dict[str, Any]) -> Dict[str, Any]:
    ts = _ts_ms()
    recv_window = 5000
    body_json = json.dumps(biz_body, separators=(",", ":"), ensure_ascii=False)
    sign = _sign_v5(ts, recv_window, body_json)
    headers = _headers_base()
    headers["X-BAPI-TIMESTAMP"] = str(ts)
    headers["X-BAPI-RECV-WINDOW"] = "5000"
    headers["X-BAPI-SIGN"] = sign
    url = f"{API.base_url}{path}"
    req = urllib.request.Request(url, data=body_json.encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

# --- Market data (linear)
def get_klines_linear(symbol: str, interval_min: int, limit: int = 300) -> List[Dict[str, Any]]:
    params = {"category": "linear", "symbol": symbol, "interval": str(interval_min), "limit": str(limit)}
    res = _public_get("/v5/market/kline", params)
    if res.get("retCode") != 0:
        raise RuntimeError(f"Kline取得失敗: {res}")
    lst = res["result"]["list"]
    rows = []
    for it in reversed(lst):
        rows.append({"start": int(it[0]), "open": float(it[1]), "high": float(it[2]), "low": float(it[3]), "close": float(it[4])})
    return rows

# --- Instruments info（qty丸め用の最小限取得）
def _get_qty_filters(symbol: str) -> Tuple[float, float]:
    """qtyStep と minOrderQty を返す"""
    res = _public_get("/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
    if res.get("retCode") != 0:
        raise RuntimeError(f"instruments-info 失敗: {res}")
    info = res["result"]["list"][0]
    lot = info.get("lotSizeFilter", {})
    qty_step = float(lot.get("qtyStep", 0.001))
    min_qty = float(lot.get("minOrderQty", 0.001))
    return qty_step, min_qty

def _round_step(x: float, step: float) -> float:
    if step <= 0: return x
    n = int(x / step)  # 切り捨て（安全側）
    return n * step

# --- Unified balance (元の実装をそのまま維持)
def get_wallet_balance_unified() -> Dict[str, Any]:
    res = _private_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    if res.get("retCode") != 0:
        raise RuntimeError(f"wallet-balance 失敗: {res}")
    return res["result"]

def _ffloat(x):
    try:
        return float(x)
    except Exception:
        return 0.0

def get_usdt_available_and_equity() -> Tuple[float, float]:
    """Unified口座のUSDT利用可能額とEquityを返す。
       1) 口座レベル totalAvailableBalance を最優先
       2) USDTコイン行から availableBalance / transferBalance / walletBalance をフォールバック
    """
    res = get_wallet_balance_unified()  # GET /v5/account/wallet-balance?accountType=UNIFIED
    acct_list = res.get("list") or []
    if not acct_list:
        return 0.0, 0.0

    acct = acct_list[0]

    # Equityは totalEquity（なければ totalWalletBalance）でOK
    equity = _ffloat(acct.get("totalEquity") or acct.get("totalWalletBalance"))

    # まずは口座レベル（USD建て）。USDT運用なら ほぼ=USDT額として扱える
    usdt_avail = _ffloat(acct.get("totalAvailableBalance"))

    # 口座レベルが取れなかった/0っぽい時だけ、USDTコイン行を参照
    if usdt_avail == 0.0:
        for c in acct.get("coin", []):
            if (c.get("coin") or "").upper() == "USDT":
                # Unifiedでは availableToWithdraw は常に ""（非推奨）なので使わない
                for k in ("availableBalance", "transferBalance", "walletBalance"):
                    v = c.get(k)
                    if v not in (None, "", "null"):
                        usdt_avail = _ffloat(v)
                        break
                break

    return usdt_avail, equity

# --- Leverage
def set_leverage_linear(symbol: str, buy_leverage: float, sell_leverage: float) -> Dict[str, Any]:
    body = {"category": "linear", "symbol": symbol, "buyLeverage": str(buy_leverage), "sellLeverage": str(sell_leverage)}
    return _private_post("/v5/position/set-leverage", body)

# --- Order（最小限の修正：reduceOnlyをbool、qtyをステップ/最小に合わせて補正）
def place_linear_market_order(symbol: str, side: str, qty: float, reduce_only: bool = False) -> Dict[str, Any]:
    step, min_qty = _get_qty_filters(symbol)
    adj_qty = max(qty, min_qty)
    adj_qty = _round_step(adj_qty, step)

    body = {
        "category": "linear",
        "symbol": symbol,
        "side": side,              # "Buy" or "Sell"
        "orderType": "Market",
        "qty": str(adj_qty),
        "reduceOnly": bool(reduce_only),  # ← 文字列"0"/"1"ではなく boolean
        "timeInForce": "IOC",
    }
    return _private_post("/v5/order/create", body)

# 互換：get_balance（あれば使われるため）
def get_balance() -> float:
    a, _ = get_usdt_available_and_equity()
    return a


# ==== Added by patch: safe reduce-only helpers ====

from typing import Optional, Dict, Any, List
import math
import time

def _floor_to_step(qty: float, step: float) -> float:
    if step and step > 0:
        return math.floor(qty / step) * step
    return qty

def _get_net_position_qty(session, symbol: str) -> float:
    try:
        r = session.get("/v5/position/list", params={"category": "linear", "symbol": symbol})
        data = r.json()
        if data.get("retCode") != 0:
            return 0.0
        items = (data.get("result") or {}).get("list") or []
        if not items:
            return 0.0
        qty = 0.0
        for it in items:
            side = it.get("side")
            sz = float(it.get("size") or 0)
            if side == "Buy":
                qty += sz
            elif side == "Sell":
                qty -= sz
        return qty
    except Exception:
        return 0.0

def safe_close_position(session, symbol: str, lot_step: float, tif: str = "IOC") -> Dict[str, Any]:
    pos = _get_net_position_qty(session, symbol)
    if abs(pos) <= 0:
        return {"status": "noop", "reason": "position_zero"}
    side = "Sell" if pos > 0 else "Buy"
    qty = _floor_to_step(abs(pos), lot_step)
    if qty <= 0:
        return {"status": "noop", "reason": "rounded_to_zero"}
    payload = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "reduceOnly": True,
        "timeInForce": tif,
        "qty": str(qty),
    }
    rr = session.post("/v5/order/create", json=payload).json()
    if rr.get("retCode") == 0:
        return {"status": "ok", "resp": rr}
    if rr.get("retCode") == 110017:
        time.sleep(0.2)
        now_pos = _get_net_position_qty(session, symbol)
        if abs(now_pos) <= 0:
            return {"status": "ok_noop", "resp": rr, "reason": "already_flat"}
        return {"status": "error", "resp": rr, "reason": "pos_nonzero_but_110017"}
    return {"status": "error", "resp": rr}

def cancel_all_reduce_only_orders(session, symbol: str) -> Dict[str, Any]:
    payload = {"category": "linear", "symbol": symbol}
    rr = session.post("/v5/order/cancel-all", json=payload).json()
    return rr

def safe_amend_reduce_only_order(session, symbol: str, order_id: str, new_qty: float, lot_step: float) -> Dict[str, Any]:
    pos = _get_net_position_qty(session, symbol)
    if abs(pos) <= 0:
        return {"status": "noop", "reason": "position_zero"}
    adj_qty = _floor_to_step(new_qty, lot_step)
    if adj_qty <= 0:
        return {"status": "noop", "reason": "rounded_to_zero"}
    payload = {
        "category": "linear",
        "symbol": symbol,
        "orderId": order_id,
        "qty": str(adj_qty),
    }
    rr = session.post("/v5/order/amend", json=payload).json()
    if rr.get("retCode") == 0:
        return {"status": "ok", "resp": rr}
    if rr.get("retCode") == 110017:
        return {"status": "ok_noop", "resp": rr, "reason": "already_flat"}
    return {"status": "error", "resp": rr}
# ==== end added ====
