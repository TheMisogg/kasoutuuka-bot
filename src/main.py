import os
import json
import time
import math
import requests
import traceback
from datetime import datetime, timedelta,timezone
import pandas as pd
from types import ModuleType
from statistics import median
from zoneinfo import ZoneInfo
from typing import Dict, Any

# .env 読み込み
from .env import load_env
load_env()

from .config import STRATEGY as S, API
from .indicators import rsi, macd, atr, sma
from .slack import notify_slack

from edge_signal_pack.indicators import adx as ws_adx
from edge_signal_pack.signal_engine import EdgeSignalEngine
EDGE_ENABLED = True
edge = None

# === Orderflow / Orderbook utilities ===
from .flow_filters import (
    fetch_recent_trades_linear, fetch_orderbook_linear,
)
# 動的ガード（今回の差分で強化済みのものを想定）
from .flow_filters_dynamic import (
    decide_entry_guard_long,
    decide_entry_guard_short,
    classify_regime,
    is_range_upper,      
    is_range_lower, 
    is_exhaustion_long,
)

# ===== bybit.py の関数名差異に自動対応（get_klines_linearが無くてもOK）=====
from typing import Optional
try:
    from .import bybit as _bx_loaded
    _bx: Optional[ModuleType] = _bx_loaded
except Exception:
    _bx = None  

_DEF_OB_DEPTH = getattr(S, "ob_depth", 50)

try:
    notify_slack(f"[DEBUG] using bybit module: {getattr(_bx, '__file__', 'N/A')}")
    notify_slack(f"[DEBUG] has place_linear_market_order? {hasattr(_bx, 'place_linear_market_order') if _bx else False}")
except Exception:
    pass



def _has(name):
    return hasattr(_bx, name) if _bx else False

# Kline取得関数（優先: get_klines_linear → 次点: get_klines → 最後: HTTPフォールバック）
if _has("get_klines_linear"):
    _get_klines_fn = getattr(_bx, "get_klines_linear")
elif _has("get_klines"):
    _get_klines_fn = getattr(_bx, "get_klines")
else:
    _get_klines_fn = None  # HTTPフォールバックを使う

_set_lev_fn        = getattr(_bx, "set_leverage_linear", None) if _bx else None
_place_linear_fn   = getattr(_bx, "place_linear_market_order", None) if _bx else None
_get_bal_equity_fn = getattr(_bx, "get_usdt_available_and_equity", None) if _bx else None
_cancel_all_fn     = getattr(_bx, "cancel_all_linear_orders", None) if _bx else None
_place_postonly_fn = getattr(_bx, "place_linear_postonly_limit", None) if _bx else None

# 旧・簡易API名（ある場合のみ使用）
_place_simple_fn   = getattr(_bx, "place_order", None) if _bx else None
_get_balance_simple= getattr(_bx, "get_balance", None) if _bx else None

# --- 反対方向エントリー禁止ガード用ヘルパー -------------------------------
_get_positions_fn = None
if _bx:
    for _name in ("get_positions_linear", "get_linear_positions", "get_position_linear", "get_positions"):
        if hasattr(_bx, _name):
            _get_positions_fn = getattr(_bx, _name)
            break

def _local_net_side(st) -> Optional[str]:
    """state.json からネットサイドを推定: 'long' / 'short' / None / 'conflict'"""
    try:
        sides = {str(p.get("side", "")).lower() for p in st.get("positions", []) if float(p.get("qty", 0)) > 0}
        if not sides:
            return None
        if "long" in sides and "short" in sides:
            return "conflict"
        return list(sides)[0]
    except Exception:
        return None

def _exchange_net_side() -> Optional[str]:
    """取引所APIからネットサイドを推定（使える関数があれば使用）"""
    if not _get_positions_fn:
        return None
    try:
        res = _get_positions_fn(S.symbol)
        # 返り値の構造を色々吸収
        if isinstance(res, dict):
            payload = res.get("result") or res.get("data") or res
            items = payload.get("list") or payload.get("positions") or payload.get("data") or []
            if isinstance(items, dict):
                items = items.get("list") or items.get("positions") or []
        elif isinstance(res, list):
            items = res
        else:
            items = []

        for it in items:
            side = str(it.get("side") or it.get("positionSide") or "").lower()
            q = it.get("size")
            if q is None: q = it.get("qty")
            if q is None: q = it.get("positionQty")
            qty = float(q or 0.0)
            if abs(qty) <= 0:
                continue
            if side in ("buy", "long"):
                return "long"
            if side in ("sell", "short"):
                return "short"
            return "long" if qty > 0 else "short"
        return None
    except Exception:
        return None

def _normalize_guard_result(res):
    # (ok, reason) / (ok, reason, overrides) の両対応
    try:
        if isinstance(res, tuple):
            if len(res) == 3:
                ok, why, overrides = res
            elif len(res) == 2:
                ok, why = res
                overrides = {}
            else:
                ok, why, overrides = False, "guard returned unexpected result", {}
        else:
            ok, why, overrides = False, "guard returned non-tuple", {}
    except Exception as e:
        ok, why, overrides = False, f"guard normalize error: {e}", {}
    return bool(ok), str(why or ""), overrides

def _apply_flip_overrides_if_any(side: str, qty: float, overrides: dict):
    """force_flip 時に qty をネット玉ぶん上乗せし、Slack 注釈文字列を返す"""
    try:
        if not overrides or not overrides.get("force_flip"):
            return qty, ""
        add = float(overrides.get("flip_additional_qty", 0.0))
        if add > 0:
            qty = float(qty) + add
        note = f"FLIP {overrides.get('flip_from','?')}→{overrides.get('flip_to','?')} +{add:.4f}"
        return qty, note
    except Exception:
        return qty, ""
    
# --- Adaptive TP/SL profile selector -----------------------------------------
def _decide_tp_sl_profile(regime: str, side: str, votes: int, ofi_z: float, S=S) -> dict:
    """
    レジーム/フローに応じて TP/SL 管理プロファイルを決定。
    返り値例: {"name":"trend_strong_long", "sl_k":1.2, "tp_rr":2.0, "be_k":0.6}
              {"name":"range", "sl_k":0.7, "tp_rr":1.0, "trail_k":0.5}
    """
    # “強トレンド＆フロー合致”の判定（票数＋OFI z）
    need_votes = int(getattr(S, "trend_votes_min", 2))
    need_ofi_z = float(getattr(S, "trend_ofi_z_min", 1.5))
    aligned = (votes >= need_votes) and (ofi_z >= need_ofi_z)

    if regime == "range":
        return {
            "name": "range",
            "sl_k": float(getattr(S, "sl_range_atr", 0.7)),
            "tp_rr": float(getattr(S, "tp_rr_range", 1.0)),
            "trail_k": float(getattr(S, "trail_k_range", 0.5)),  # 逆指値トレール幅
        }

    if regime == "trend_up" and aligned:
        if side == "LONG":
            return {
                "name": "trend_strong_long",
                "sl_k": float(getattr(S, "sl_trend_long_atr", 1.2)),
                "tp_rr": float(getattr(S, "tp_rr_trend_long", 2.0)),
                "be_k": float(getattr(S, "be_k_trend_long", 0.6)),  # +0.6ATR 到達で建値へ
            }
        else:
            # 上昇トレンド時のショートは非対称（SLを広げる）
            return {
                "name": "trend_strong_short",
                "sl_k": float(getattr(S, "sl_trend_short_atr", 1.3)),
                "tp_rr": float(getattr(S, "tp_rr_trend_short", 2.0)),
                "be_k": float(getattr(S, "be_k_trend_short", 0.6)),
            }

    # 上記以外はニュートラル扱い
    return {
        "name": "neutral",
        "sl_k": float(getattr(S, "sl_neutral_atr", 1.0)),
        "tp_rr": float(getattr(S, "tp_rr_neutral", 1.5)),
        "be_k": float(getattr(S, "be_k_neutral", 0.5)),
    }

# --- ATRヒストリ更新（stateに保存） ------------------------------------------
def _update_atr_hist(st: dict, atr_value: float, max_len: int = 200) -> list[float]:
    hist = list(st.get("atr_hist", []))
    try:
        hist.append(float(atr_value))
    except Exception:
        return hist
    if len(hist) > max_len:
        hist = hist[-max_len:]
    st["atr_hist"] = hist
    return hist

# --- 動的クールダウン計算 -----------------------------------------------------
def _dynamic_cooldown_minutes(
    st: dict,
    base_min: int,
    *,
    short_win: int = 12,
    long_win: int = 72,
    floor_min: int = 5,
    cap_min: int = 30,
) -> tuple[int, dict]:
    """
    cooldown = clip( int(base * (median(ATR_last_short)/median(ATR_last_long))), floor, cap)
    short_win: 直近窓（5m足×12=約1h）
    long_win : 比較窓（5m足×72=約6h）
    """
    hist: list[float] = list(st.get("atr_hist", []))
    meta = {"reason": "", "short_med": None, "long_med": None, "ratio": None}
    if len(hist) < max(short_win, long_win):
        meta["reason"] = "insufficient_atr_hist"
        return int(base_min), meta
    sm = float(median(hist[-short_win:]))
    lm = float(median(hist[-long_win:]))
    meta.update({"short_med": sm, "long_med": lm})
    if lm <= 1e-12:
        meta["reason"] = "zero_long_med"
        return int(base_min), meta
    ratio = sm / lm
    dyn = int(max(floor_min, min(cap_min, round(base_min * ratio))))
    meta.update({"ratio": ratio, "dyn": dyn})
    return dyn, meta

def _strong_flow_override(edge, edge_votes: int, S=S) -> tuple[bool, str]:
    """
    レンジ日でも“でかい魚”を通すための例外判定。
    しきい値は regime 専用が無ければ cooldown 用を使う（後方互換）。
    """
    try:
        met = edge.get_metrics_snapshot() if (edge and hasattr(edge, "get_metrics_snapshot")) else {}
    except Exception:
        met = {}
    def _f(x, t=float, d=0): 
        try: return t(x)
        except Exception: return d

    ofi_z     = _f(met.get("ofi_z", 0.0), float, 0.0)
    cons_buy  = _f(met.get("cons_buy", 0), int, 0)
    cons_sell = _f(met.get("cons_sell", 0), int, 0)

    # まず regime_* を探し、無ければ cooldown_* を使う
    th_ofi   = float(getattr(S, "regime_override_ofi_z",
                       getattr(S, "cooldown_override_ofi_z", 2.2)))
    th_cons  = int(getattr(S, "regime_override_cons",
                       getattr(S, "cooldown_override_cons", 3)))
    th_votes = int(getattr(S, "regime_override_votes",
                       getattr(S, "cooldown_override_votes", 3)))

    strong = (abs(ofi_z) >= th_ofi) or (max(cons_buy, cons_sell) >= th_cons) or (int(edge_votes or 0) >= th_votes)
    note = f"OFI z={ofi_z:.2f}, cons={max(cons_buy,cons_sell)}, votes={edge_votes}"
    return strong, note

# --- 強フローによる“クールダウン解除”判定 ------------------------------------
def _cooldown_override_by_flow(edge, S) -> tuple[bool, str]:
    """
    abs(ofi_z) >= th_ofi  or  cons_buy|cons_sell >= th_cons で override。
    """
    try:
        met = edge.get_metrics_snapshot() if (edge and hasattr(edge, "get_metrics_snapshot")) else {}
    except Exception:
        met = {}
    def _f(x, t=float, d=0):
        try: return t(x)
        except Exception: return d
    ofi_z   = _f(met.get("ofi_z", 0.0), float, 0.0)
    cons_buy= _f(met.get("cons_buy", 0),   int,   0)
    cons_sell=_f(met.get("cons_sell", 0),  int,   0)

    th_ofi  = float(getattr(S, "cooldown_override_ofi_z", 2.2))
    th_cons = int(getattr(S, "cooldown_override_cons", 3))

    if abs(ofi_z) >= th_ofi or max(cons_buy, cons_sell) >= th_cons:
        return True, f"cooldown_override(ofi_z={ofi_z:.2f}, cons={max(cons_buy, cons_sell)})"
    return False, ""


def _guard_opposite_entry(requested_side: str, st) -> tuple:
    """
    反対側ポジションがあるときのガード。
   - 既定（allow_atomic_flip=False）は常にブロック
   - allow_atomic_flip=True のときだけ、強フローかつ最小保有時間/反転間隔を満たせば FLIP を許可
    戻り値は (ok, reason) 互換。フリップ時は (ok, reason, overrides) を返す。
    """
    global S
    # forbidがFalseなら素通り
    if not bool(getattr(S, "forbid_opposite_entry", True)):
        return True, ""
    allow_atomic = bool(getattr(S, "allow_atomic_flip", False))
    min_hold_min  = int(getattr(S, "min_hold_minutes_after_entry", 0))
    min_flip_min  = int(getattr(S, "min_flip_interval_min", 0))
    # --- ヘルパ ---
    def _net_side_local(_st) -> str:
        q = 0.0
        try:
            for p in (_st or {}).get("positions", []):
                side = (p.get("side") or "").lower()
                size = float(p.get("size") or p.get("qty") or 0.0)
                if side == "long":
                    q += size
                elif side == "short":
                    q -= size
        except Exception:
            pass
        if q > 0: return "long"
        if q < 0: return "short"
        return "flat"

    def _net_side_any(_st) -> str:
        # main側に _exchange_net_side があれば優先
        try:
            return _exchange_net_side() or _net_side_local(_st)  # noqa: F821
        except Exception:
            return _net_side_local(_st)

    def _edge_obj(_st):
        try:
            if isinstance(_st, dict) and _st.get("edge") is not None:
                return _st.get("edge")
        except Exception:
            pass
        try:
            return globals().get("edge")
        except Exception:
            return None

    def _edge_metrics(edge):
        met = {}
        try:
            if edge and hasattr(edge, "get_metrics_snapshot"):
                met = edge.get_metrics_snapshot() or {}
        except Exception:
            met = {}
        def _f(x, t=float):
            try: return t(x)
            except Exception: return 0
        return {
            "ofi_z": _f(met.get("ofi_z", 0.0), float),
            "cons_buy": _f(met.get("cons_buy", 0), int),
            "cons_sell": _f(met.get("cons_sell", 0), int),
            "cvd_slope_z": _f(met.get("cvd_slope_z", 0.0), float),
        }

    def _should_flip(side: str, edge) -> tuple[bool, dict]:
        # 既定では反転を許さない
        if not allow_atomic:
            return False, {}
        if not bool(getattr(S, "flip_enable", True)):
            return False, {}        
        m = _edge_metrics(edge)
        ofi, cb, cs, cvd = m["ofi_z"], m["cons_buy"], m["cons_sell"], m["cvd_slope_z"]
        th_ofi = float(getattr(S, "flip_ofi_z", 2.0))
        th_cons = int(getattr(S, "flip_cons", 3))
        th_cvd = float(getattr(S, "flip_cvd_z", 1.5))
        votes = 0
        if side == "LONG":
            if ofi >= th_ofi: votes += 1
            if cb  >= th_cons: votes += 1
            if cvd >= th_cvd:  votes += 1
        else:  # SHORT
            if ofi <= -th_ofi: votes += 1
            if cs  >= th_cons: votes += 1
            if cvd <= -th_cvd: votes += 1
        need = int(getattr(S, "flip_votes_needed", 2))
        return (votes >= need), {"metrics": m}

    def _min_hold_ok() -> bool:
        if min_hold_min <= 0:
            return True
        iso = st.get("last_entry_time")
        if not iso:
            return True
        try:
            dt = datetime.fromisoformat(iso)
            return (datetime.utcnow() - dt) >= timedelta(minutes=min_hold_min)
        except Exception:
            return True

    def _flip_interval_ok() -> bool:
        if min_flip_min <= 0:
            return True
        iso = st.get("last_flip_time")
        if not iso:
            return True
        try:
            dt = datetime.fromisoformat(iso)
            return (datetime.utcnow() - dt) >= timedelta(minutes=min_flip_min)
        except Exception:
            return True

    def _net_qty_abs(_st) -> float:
        q = 0.0
        try:
            for p in (_st or {}).get("positions", []):
                side = (p.get("side") or "").lower()
                size = float(p.get("size") or p.get("qty") or 0.0)
                if side == "long":  q += size
                elif side == "short": q -= size
        except Exception:
            pass
        return abs(q)

    # --- 本体 ---
    net = _net_side_any(st)
    if net in ("", None, "flat"):
        return True, ""

    conflict = (net == "long" and requested_side == "SHORT") or (net == "short" and requested_side == "LONG")
    if not conflict:
        if net == "conflict":
            return False, "ローカルstateにlong/short混在→新規禁止（state整合が必要）"
        return True, ""

    # 反対側ポジ保有中：
    # ---- 反対側ポジ保有中：既定はブロック。許可時のみ FLIP を検討 ----
    allow_atomic = bool(getattr(S, "allow_atomic_flip", False))
    min_hold_min = int(getattr(S, "min_hold_minutes_after_entry", 0))
    min_flip_min = int(getattr(S, "min_flip_interval_min", 0))

    def _min_hold_ok() -> bool:
        if min_hold_min <= 0: return True
        iso = st.get("last_entry_time")
        if not iso: return True
        try:
            dt = datetime.fromisoformat(iso)
            return (datetime.utcnow() - dt) >= timedelta(minutes=min_hold_min)
        except Exception:
            return True

    def _flip_interval_ok() -> bool:
        if min_flip_min <= 0: return True
        iso = st.get("last_flip_time")
        if not iso: return True
        try:
            dt = datetime.fromisoformat(iso)
            return (datetime.utcnow() - dt) >= timedelta(minutes=min_flip_min)
        except Exception:
            return True

    if allow_atomic and _min_hold_ok() and _flip_interval_ok():
        flip_ok, meta = _should_flip(requested_side, _edge_obj(st))
        if flip_ok:
            overrides = {
                "force_flip": True,
                "flip_additional_qty": _net_qty_abs(st),  # いまのネット玉ぶん
                "flip_from": net.upper(),
                "flip_to": requested_side,
                "flip_metrics": meta.get("metrics", {}),
            }
            return True, f"FLIP: {net}→{requested_side.lower()} by strong_flow", overrides

    # フリップ不可 → ブロック
    if net == "long" and requested_side == "SHORT":
        return False, "反対方向ポジション保有中（net=long）"
    if net == "short" and requested_side == "LONG":
        return False, "反対方向ポジション保有中（net=short）"
    if net == "conflict":
        return False, "ローカルstateにlong/short混在→新規禁止（state整合が必要）"
    return True, ""

# ---------------------------------------------------------------------------
def _cleanup_positions_after_flip(side: str, state: dict):
    """成行フリップ後、ローカルstateから反対サイドを除去して 'conflict' を防ぐ。"""
    opp = "short" if side == "LONG" else "long"
    try:
        ps = state.get("positions", [])
        state["positions"] = [p for p in ps if (p.get("side") or "").lower() != opp]
    except Exception:
        pass


STATE_FILE = "state.json"

# ---------- State ----------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "positions": [],
            "last_report_date": None,
            "last_week_report": None,
            "last_kline_start": None,
            "leverage_set": False,
            "last_entry_time": None,
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def update_trading_state(state: dict, pnl: float, is_win: bool):
    """取引後の状態更新"""
    # 日次PNL更新
    state["daily_pnl"] = state.get("daily_pnl", 0) + pnl
    
    # 連続勝敗更新
    if is_win:
        state["consecutive_losses"] = 0
        state["consecutive_wins"] = state.get("consecutive_wins", 0) + 1
    else:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        state["consecutive_wins"] = 0
    
    # ニュートラル取引カウント
    regime = state.get("last_regime", "neutral")
    if regime == "neutral":
        state["neutral_trade_count"] = state.get("neutral_trade_count", 0) + 1

# ---------- Kline (堅牢HTTPフォールバック) ----------

def _fetch_bybit_json(url: str, params: dict, timeout: int = 10, max_retry: int = 5):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ja,en;q=0.9",
    }
    sleep_base = 0.7
    last_err = None
    for i in range(1, max_retry + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            text = r.text
            try:
                data = r.json()
            except Exception as je:
                last_err = je
                time.sleep(sleep_base * i)
                continue
            if isinstance(data, dict) and data.get("retCode") != 0:
                last_err = RuntimeError(f"retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
                time.sleep(sleep_base * i)
                continue
            return data
        except Exception as e:
            last_err = e
            time.sleep(sleep_base * i)
    raise RuntimeError(f"Bybit API fetch失敗: {last_err}")

def http_get_klines(symbol: str, interval_min: int, limit: int = 300):
    hosts = (API.base_url,) + tuple(getattr(API, "alt_hosts", ()))
    params = {"category": S.category, "symbol": symbol, "interval": str(int(interval_min)), "limit": str(int(limit))}
    interval_ms = int(interval_min) * 60_000
    last_exc = None
    for host in hosts:
        url = f"{host}/v5/market/kline"
        try:
            data = _fetch_bybit_json(url, params)
            rows = []
            for it in reversed(data["result"]["list"]):
                ts = int(it[0])
                start_ts = ts - interval_ms + 1
                rows.append({"start": start_ts, "open": float(it[1]), "high": float(it[2]), "low": float(it[3]), "close": float(it[4])})
            if not rows:
                raise RuntimeError("空のKlineが返されました")
            return rows
        except Exception as e:
            last_exc = e
            continue
    raise RuntimeError(f"Kline取得に全ホストで失敗: {last_exc}")


def get_klines_any(symbol: str, interval_min: int, limit: int = 300):
    if _get_klines_fn:
        try:
            rows = _get_klines_fn(symbol, int(interval_min), int(limit))
            if rows and isinstance(rows[0], dict) and "start" in rows[0]:
                return rows
            adapted = []
            for r in rows:
                ts = int(r.get("timestamp") or r.get("start") or 0)
                if ts < 10**12:
                    ts *= 1000
                adapted.append({"start": ts, "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])})
            return adapted
        except Exception:
            pass
    return http_get_klines(symbol, interval_min, limit)

def get_1h_trend(symbol: str) -> dict:
    """1時間足のトレンド方向を確認"""
    try:
        rows_1h = get_klines_any(symbol, 60, 100)  # 1時間足
        if not rows_1h:
            return {"trend": "neutral", "sma": 0.0}
        
        closes = [r["close"] for r in rows_1h]
        sma_1h = sma(closes, S.trend_confirmation_sma_period)
        current_price = closes[-1]
        sma_value = sma_1h[-1] if sma_1h else current_price
        
        if current_price > sma_value * 1.005:  # 0.5%以上上なら上昇トレンド
            return {"trend": "uptrend", "sma": sma_value}
        elif current_price < sma_value * 0.995:  # 0.5%以下なら下降トレンド
            return {"trend": "downtrend", "sma": sma_value}
        else:
            return {"trend": "neutral", "sma": sma_value}
    except Exception as e:
        return {"trend": "neutral", "sma": 0.0}
    
# エントリー条件チェック関数を追加
def check_enhanced_entry_conditions(ctx: dict, ind: dict, S) -> tuple[bool, str]:
    """
    強化されたエントリー条件チェック
    returns: (ok, reason)
    """
    price = ctx.get("price", 0)
    rsi_val = ind.get("rsi", [0])[-1] if ind.get("rsi") else 50
    atr_val = ind.get("atr", [0])[-1] if ind.get("atr") else 0
    atr_hist = ctx.get("atr_hist", [])
    
    # RSI過熱度チェック
    if ctx.get("side_for_entry") == "LONG" and rsi_val > S.rsi_overbought:
        return False, f"RSI過熱度: {rsi_val:.1f} > {S.rsi_overbought}"
    
    if ctx.get("side_for_entry") == "SHORT" and rsi_val < S.rsi_oversold:
        return False, f"RSI過熱度: {rsi_val:.1f} < {S.rsi_oversold}"
    
    # ボラティリティフィルター
    if S.use_atr_filter and atr_hist:
        avg_atr = sum(atr_hist[-20:]) / min(20, len(atr_hist))  # 直近20本の平均ATR
        if avg_atr > 0 and atr_val < avg_atr * S.min_atr_ratio_to_avg:
            return False, f"ボラティリティ不足: ATR{atr_val:.4f} < 平均の{S.min_atr_ratio_to_avg*100}%"
    
    # 1時間足トレンド確認
    if S.use_1h_trend_confirmation:
        trend_1h = get_1h_trend(S.symbol)
        current_side = ctx.get("side_for_entry", "")
        
        if current_side == "LONG" and trend_1h["trend"] == "downtrend":
            return False, "1時間足トレンド不一致(下降トレンド中にLONG)"
        if current_side == "SHORT" and trend_1h["trend"] == "uptrend":
            return False, "1時間足トレンド不一致(上昇トレンド中にSHORT)"
    
    return True, "条件OK"

def _fill_price_from_res(res: dict, fallback: float) -> float:
    try:
        r = res.get("result") or {}
        # どれかに入っていれば拾う（Bybit統合口座の典型）
        return float(
            r.get("avgPrice") or
            r.get("price") or
            (r.get("list", [{}])[0].get("avgPrice"))  # list形式の場合
        )
    except Exception:
        return float(fallback)

# ---------- 指標計算 ----------

def compute_indicators(rows):
    closes = [r["close"] for r in rows]
    highs  = [r["high"] for r in rows]
    lows   = [r["low"] for r in rows]
    rsi_vals = rsi(closes, int(getattr(S, "rsi_period", 14)))
    macd_line, signal_line, _ = macd(closes,
                                     int(getattr(S, "macd_fast", 12)),
                                     int(getattr(S, "macd_slow", 26)),
                                     int(getattr(S, "macd_signal", 9)))
    atr_vals = atr(highs, lows, closes, int(getattr(S, "atr_period", 14)))
    sma10 = sma(closes, 10)
    sma50 = sma(closes, 50)
    return {
        "rsi": rsi_vals, "macd": macd_line, "signal": signal_line, "atr": atr_vals,
        "sma10": sma10, "sma50": sma50, "close": closes, "high": highs, "low": lows,
        "start": [r["start"] for r in rows]
    }

# ===== 可観測性 / 日次集計 =====================================================

def _jst_now():
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Tokyo"))

def _jst_date_key(dt: datetime | None = None) -> str:
    dt = dt or _jst_now()
    return dt.strftime("%Y-%m-%d")

def _get_daily_bucket(st: Dict[str, Any], date_key: str | None = None) -> Dict[str, Any]:
    """state['obs']['daily'][date_key] に日次の集計バケットを確保して返す"""
    if "obs" not in st: st["obs"] = {}
    if "daily" not in st["obs"]: st["obs"]["daily"] = {}
    dk = date_key or _jst_date_key()
    if dk not in st["obs"]["daily"]:
        st["obs"]["daily"][dk] = {
            "skips": {
                "no_consensus": 0,
                "regime_not_ok": 0,
                "cooldown": 0,
                "opposite_guard": 0,
                "min_notional": 0,
                "max_positions": 0,
                "other": 0,
            },
            "trades": {
                "count": 0,
                "wins": 0,
                "losses": 0,
                "rr_sum": 0.0,      # 符号付きRR合計（勝ち:+ / 負け:-）
                "rr_count": 0,
                "flip_attempts": 0,
                "flip_wins": 0,
                "max_losing_streak": 0,
                "cur_losing_streak": 0,
            },
            "sent": False,  # その日のサマリー送信済みフラグ
        }
    return st["obs"]["daily"][dk]

def _bump_skip(st: Dict[str, Any], reason: str, n: int = 1):
    b = _get_daily_bucket(st)
    key = reason if reason in b["skips"] else "other"
    b["skips"][key] = int(b["skips"].get(key, 0)) + n

def _on_new_entry(st: Dict[str, Any], *, is_flip: bool = False):
    b = _get_daily_bucket(st)
    b["trades"]["count"] += 1
    if is_flip: b["trades"]["flip_attempts"] += 1

def _on_close_trade(st: Dict[str, Any], *, entry: float, exit_: float, side: str, risk_sl_dist: float, was_flip: bool = False):
    """ポジション決済時に勝敗とRRを更新"""
    if risk_sl_dist <= 1e-12:
        rr = 0.0
    else:
        profit = (exit_ - entry) if side == "long" else (entry - exit_)
        rr = (profit / risk_sl_dist)

    b = _get_daily_bucket(st)
    b["trades"]["rr_sum"]   += float(rr)
    b["trades"]["rr_count"] += 1

    if rr > 0:
        b["trades"]["wins"] += 1
        b["trades"]["cur_losing_streak"] = 0
        if was_flip: b["trades"]["flip_wins"] += 1
    else:
        b["trades"]["losses"] += 1
        b["trades"]["cur_losing_streak"] = int(b["trades"]["cur_losing_streak"]) + 1
        if b["trades"]["cur_losing_streak"] > b["trades"]["max_losing_streak"]:
            b["trades"]["max_losing_streak"] = b["trades"]["cur_losing_streak"]

def _maybe_send_daily_summary(st: Dict[str, Any]):
    """JST 23:00 でその日の集計を一回だけ Slack へ送る"""
    jst = _jst_now()
    dk  = _jst_date_key(jst)
    b   = _get_daily_bucket(st, dk)
    if jst.hour < 23 or b.get("sent"):
        return

    # 集計
    skips = b["skips"]; trades = b["trades"]
    total_skips = sum(int(v) for v in skips.values())
    total_trades = int(trades["count"])
    total_events = total_skips + total_trades if (total_skips + total_trades) > 0 else 1
    win_rate = (trades["wins"] / max(1, total_trades)) * 100.0
    avg_rr = (trades["rr_sum"] / max(1, trades["rr_count"]))

    # 内訳を%付きで整形
    def pct(n): return f"{(n / total_events)*100:.1f}%"
    lines = []
    lines.append(f"📊 *日次サマリー* {dk} (JST)")
    lines.append(f"・機会総数: {total_events} = スキップ {total_skips} + 取引 {total_trades}")
    lines.append("・スキップ内訳:")
    for k in ("no_consensus","regime_not_ok","cooldown","opposite_guard","min_notional","other"):
        v = int(skips.get(k,0))
        lines.append(f"  - {k}: {v} ({pct(v)})")
    lines.append("・トレード:")
    lines.append(f"  - 実トレード数: {total_trades}")
    lines.append(f"  - 勝率: {win_rate:.1f}%  ({trades['wins']}/{total_trades})")
    lines.append(f"  - 平均RR: {avg_rr:.2f}  （正値=平均利益RR / 負値=平均損失RR）")
    lines.append(f"  - 最大連敗: {int(trades['max_losing_streak'])}")
    lines.append(f"  - フリップ: {int(trades.get('flip_attempts',0))} 回 / 成功 {int(trades.get('flip_wins',0))} 回")
    if int(trades.get("flip_attempts",0)) > 0:
        sr = (trades.get("flip_wins",0) / max(1, trades.get("flip_attempts",0))) * 100.0
        lines.append(f"    ・成功率: {sr:.1f}%")

    notify_slack("\n".join(lines))
    b["sent"] = True

# ---------- 確定足ユーティリティ ----------
_last_wait_start = None

def get_latest_closed_index(rows, interval_min, safety_ms=1500):
    if not rows:
        return None
    now_ms = int(time.time() * 1000)
    interval_ms = int(interval_min) * 60_000
    for i in range(len(rows)-1, -1, -1):
        start = int(rows[i]["start"])
        if start + interval_ms <= now_ms - safety_ms:
            return i
    return None

def log_wait_once(current_start_ms):
    global _last_wait_start
    if _last_wait_start != current_start_ms:
        print(f"[WAIT] Candle not closed yet start={current_start_ms}")
        _last_wait_start = current_start_ms

# ---------- 残高 ----------

def get_free_and_equity():
    if _get_bal_equity_fn:
        try:
            f, e = _get_bal_equity_fn()
            return float(f), float(e)
        except Exception as e:
            print(f"[WARN] get_usdt_available_and_equity失敗: {e}")
    if _get_balance_simple:
        try:
            bal = _get_balance_simple()
            return float(bal), float(bal)
        except Exception as e:
            print(f"[WARN] get_balance失敗: {e}")
    return 0.0, 0.0


def set_leverage_if_possible():
    if _set_lev_fn:
        try:
            res = _set_lev_fn(S.symbol, float(S.leverage), float(S.leverage))
            notify_slack(f"⚙️ レバレッジ設定: {str(res)[:160]}")
            return True
        except Exception as e:
            notify_slack(f":x: レバレッジ設定失敗: {e}")
    return False


def est_margin_ratio(usdt_free: float, positions, last_price: float) -> float:
    pos_value = sum([float(p["qty"]) * float(last_price) for p in positions])
    used_margin = pos_value / float(S.leverage) if pos_value > 0 else 0.0
    fees_locked = sum([float(p.get("buy_fee", 0.0)) for p in positions])
    equity = usdt_free + pos_value - fees_locked
    if used_margin == 0:
        return 1.0
    return equity / used_margin

# ---------- 起動/レポート ----------

def send_startup_status(state):
    try:
        notify_slack("🟢 起動: プロセス開始（.env 読み込み済み）")
    except Exception as e:
        print(f"[Slackテスト失敗] {e}")
    try:
        usdt_free, equity = get_free_and_equity()
    except Exception as e:
        notify_slack(f":x: 起動時: 残高取得失敗 → {e}")
        usdt_free, equity = 0.0, 0.0
    try:
        rows = get_klines_any(S.symbol, int(S.interval_min), 2)
        last_price = rows[-1]["close"] if rows else float("nan")
    except Exception as e:
        notify_slack(f":x: 起動時: Kline取得失敗 → {e}")
        last_price = float("nan")

    notify_slack(
        "🚀 起動ステータス（Unified/Derivatives）\n"
        f"・シンボル: {S.symbol} / 期間: {int(S.interval_min)}m\n"
        f"・レバ: x{int(float(S.leverage))} / 同時最大: {int(S.max_positions)}\n"
        f"・証拠金比率: {int(S.position_pct*100)}% / 最小発注: {float(S.min_notional_usdt):.2f} USDT\n"
        f"・USDTフリー: {usdt_free:.4f} / Equity: {equity:.4f}\n"
        f"・現在価格: {last_price:.4f}\n"
        f"・復元ポジ数: {len(state.get('positions', []))}"
    )

# ---------- メインループ ----------

def run_loop():
    state = load_state()
    state = state or {}
    
    # === ニュートラル取引カウントのリセット処理 ===
    # 状態初期化時に追加
    if "last_neutral_reset" not in state:
        state["last_neutral_reset"] = datetime.utcnow().isoformat()

    # 1時間ごとにリセット
    last_reset = datetime.fromisoformat(state.get("last_neutral_reset", datetime.utcnow().isoformat()))
    if (datetime.utcnow() - last_reset).total_seconds() >= 3600:
        state["neutral_trade_count"] = 0
        state["last_neutral_reset"] = datetime.utcnow().isoformat()
        save_state(state)  # リセット時に状態を保存

    # OB 持続偏りの履歴（ask/bid の移動平均を取る）
    state.setdefault("ob_hist", [])
    
    # ---- Orderbook ask/bid 比を簡易算出（上位 depth で合計）----
    def _compute_ask_bid_ratio(book: dict, depth: int = 50) -> float:
        try:
            asks = book.get("asks", [])[:depth]
            bids = book.get("bids", [])[:depth]
            asum = sum(float(q) for _, q in asks) or 1e-9
            bsum = sum(float(q) for _, q in bids) or 1e-9
            return float(asum / bsum)
        except Exception:
            return 1.0
        
    realized_pnl_log = []
    last_handled_kline = state.get("last_kline_start")

    if not state.get("leverage_set"):
        if set_leverage_if_possible():
            state["leverage_set"] = True
            save_state(state)

    send_startup_status(state)
    notify_slack("✅ 監視開始（確定足待ち）")

        # === EdgeSignalEngine 起動（板/約定/清算のWS） ===
    global edge
    if EDGE_ENABLED and edge is None:
        try:
            edge = EdgeSignalEngine(
                symbol=S.symbol,
                timeframe_base=f"{int(S.interval_min)}m",
                jst_active_hours=((16,0,0),(2,0,0)),
            )
            edge.start()
            notify_slack(":electric_plug: EdgeSignalEngine 起動")
            edge.is_active_hours_jst = lambda: True  # ← 時間帯ふぃるふぃるたー無効化
        except Exception as e:
            notify_slack(f":x: EdgeSignalEngine 初期化失敗: {e}")

    backoff = 1
    while True:
        try:
        # Kline取得（失敗時は指数バックオフ）
            while True:
                try:
                    rows = get_klines_any(S.symbol, int(S.interval_min), int(getattr(S, "lookback_limit", 300)))
                    backoff = 1
                    break
                except Exception as e:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)

            if not rows:
                time.sleep(float(S.poll_interval_sec))
                continue

            closed_idx = get_latest_closed_index(rows, int(S.interval_min))
            if closed_idx is None:
                log_wait_once(rows[-1]["start"])
                time.sleep(float(S.poll_interval_sec))
                continue

            last_start = rows[closed_idx]["start"]
            if last_handled_kline == last_start:
                time.sleep(float(S.poll_interval_sec))
                continue

            relax_note = ""

            rows_for_calc = rows[:closed_idx+1]
            ind = compute_indicators(rows_for_calc)
            idx = closed_idx
            c   = float(ind["close"][idx])
            h   = float(ind["high"][idx])
            l   = float(ind["low"][idx])
            r   = float(ind["rsi"][idx])
            m   = float(ind["macd"][idx])
            sgn = float(ind["signal"][idx])
            a   = float(ind["atr"][idx])
            s10 = float(ind["sma10"][idx])
            s50 = float(ind["sma50"][idx])

            # === EdgeSignal: レジーム更新（ATR%/ADX） ===
            sig = None
            edge_votes = 0
            ofi_z = 0.0
            adx_val = 0.0
            cons_buy = 0         
            cons_sell = 0

            edge_obj = state.get("edge") or edge
            strong_flow, strong_note = _strong_flow_override(edge_obj, int(edge_votes or 0), S)

            
            if EDGE_ENABLED and edge:
                try:
                    df_calc = pd.DataFrame(
                        [{"open": rr["open"], "high": rr["high"], "low": rr["low"], "close": rr["close"]}
                        for rr in rows_for_calc[-200:]]
                    )
                    edge.update_regime(df_calc)
                    try:
                        adx_val = float(ws_adx(df_calc, period=int(getattr(S, "adx_period", 14))))
                    except Exception:
                        adx_val = 0.0
                except Exception:
                    pass

            # ベース条件（あなたの元ロジックに準拠）
            # 粗フィルタ（ニュートラル化：LONG/SHORTともEdgeへ回す）
            _nan = (
                math.isnan(s10) or math.isnan(s50) or
                math.isnan(m)   or math.isnan(sgn) or
                math.isnan(r)   or math.isnan(a)
            )
            min_atr_usd = float(getattr(S, "min_atr_usd", 0.0))  # 任意: 足りなければ0.0のままでOK
            cond_base = (not _nan) and (a >= min_atr_usd)

            # 後方互換：既存の 'elif cond_entry:' をそのまま使えるようにする
            cond_entry = cond_base

                # 必要ならデバッグ（有効化は config.py の STRATEGY に debug_entry_filters=True を足す）
            if bool(getattr(S, "debug_entry_filters", False)) and not cond_base:
                try:
                    notify_slack(
                        f"ℹ️ スキップ: 粗フィルタ未充足 | nan={_nan} atr={a:.4f} < min_atr_usd={min_atr_usd:.4f}"
                    )
                except Exception:
                    pass
            
            # ※ 建値移動・レンジ用トレールは “ポジの be_k / trail_k” を使う
            still_open = []
            for p in state.get("positions", []):
                p_side = p.get("side", "long")
                ep     = float(p["entry_price"])
                qty    = float(p["qty"])
                buy_fee= float(p.get("buy_fee", 0.0))

                in_profit = (c - ep) if p_side == "long" else (ep - c)

                # 1) 建値移動（be_kが入っていればそれを優先）
                be_k = float(p.get("be_k", getattr(S, "move_be_atr_k", 1.0)))
                if be_k > 0 and bool(getattr(S, "use_move_to_be", False)):
                    try:
                        if in_profit >= be_k * a and not p.get("sl_to_be"):
                            p["sl_price"] = ep
                            p["sl_to_be"] = True
                            notify_slack(f"🧷 SL→建値 | {ep:.4f} ({p_side}) be_k={be_k}")
                    except Exception:
                        pass

                # 2) レンジ時トレール（trail_k>0 のポジだけ可動）
                trail_k = float(p.get("trail_k", 0.0))
                if trail_k > 0:
                    try:
                        if p_side == "long":
                            cand = c - trail_k * a
                            # 既存SLより不利にならないよう、片側だけ更新
                            p["sl_price"] = max(float(p.get("sl_price", ep - 9e9)), cand)
                        else:
                            cand = c + trail_k * a
                            p["sl_price"] = min(float(p.get("sl_price", ep + 9e9)), cand)
                    except Exception:
                        pass

                closed = False
                # 利確
                if ((p_side == "long" and h >= float(p["tp_price"])) or
                    (p_side == "short" and l <= float(p["tp_price"]))) and _place_linear_fn:
                    qty = float(p["qty"]) ; tp = float(p["tp_price"]) ; ep = float(p["entry_price"]) ; buy_fee = float(p.get("buy_fee", 0.0))
                    try:
                        close_side = "Sell" if p_side == "long" else "Buy"
                        res = _place_linear_fn(S.symbol, close_side, qty, True)
                        if isinstance(res, dict) and res.get("retCode") == 0:
                            exit_notional = qty * tp
                            if p_side == "long":
                                gross = (tp - ep) * qty
                            else:
                                gross = (ep - tp) * qty
                            sell_fee = exit_notional * float(getattr(S, "taker_fee_rate", 0.0007))
                            net = gross - buy_fee - sell_fee
                            realized_pnl_log.append(net)
                            notify_slack(f"✅ 利確({p_side}): {net:+.2f} USDT | {ep:.4f}→{tp:.4f} | Qty {qty:.4f}")

                            update_trading_state(state, net, net > 0)

                            exit_price = _fill_price_from_res(res, tp)  # 実約定があればそれ、無ければtp
                            risk_sl_dist = (ep - float(p["sl_price"])) if p_side == "long" else (float(p["sl_price"]) - ep)

                            _on_close_trade(
                                state,
                                entry=float(p["entry_price"]),
                                exit_=float(exit_price),   # その決済価格の変数に合わせてください
                                side=str(p.get("side","long")),
                                risk_sl_dist=float(p.get("risk_sl_dist", abs(float(p["entry_price"]) - float(p["sl_price"])))),
                                was_flip=bool(p.get("flip", False)),
                            )
                            closed = True
                        else:
                            notify_slack(f":x: 決済失敗: {res}")
                    except Exception as e:
                        notify_slack(f":x: 決済APIエラー: {e}")
                # 損切
                if not closed and (
                    (p_side == "long"  and l <= float(p.get("sl_price", -1))) or
                    (p_side == "short" and h >= float(p.get("sl_price",  1e9)))
                ):
                    qty = float(p["qty"]) ; sl = float(p["sl_price"]) ; ep = float(p["entry_price"]) ; buy_fee = float(p.get("buy_fee", 0.0))
                    try:
                        if _place_linear_fn:
                            close_side = "Sell" if p_side == "long" else "Buy"
                            res = _place_linear_fn(S.symbol, close_side, qty, True)
                            if isinstance(res, dict) and res.get("retCode") == 0:
                                exit_notional = qty * sl
                                if p_side == "long":
                                    gross = (sl - ep) * qty
                                else:
                                    gross = (ep - sl) * qty
                                sell_fee = exit_notional * float(getattr(S, "taker_fee_rate", 0.0007))
                                net = gross - buy_fee - sell_fee
                                realized_pnl_log.append(net)
                                notify_slack(f"🛑 損切({p_side}): {net:+.2f} USDT | {ep:.4f}→{sl:.4f} | Qty {qty:.4f}")

                                update_trading_state(state, net, net > 0)

                                exit_price = _fill_price_from_res(res, sl)
                                risk_sl_dist = (ep - float(p["sl_price"])) if p_side == "long" else (float(p["sl_price"]) - ep)

                                _on_close_trade(
                                    state,
                                    entry=float(p["entry_price"]),
                                    exit_=float(exit_price),   # その決済価格の変数に合わせてください
                                    side=str(p.get("side","long")),
                                    risk_sl_dist=float(p.get("risk_sl_dist", abs(float(p["entry_price"]) - float(p["sl_price"])))),
                                    was_flip=bool(p.get("flip", False)),
                                )
                                closed = True
                            else:
                                notify_slack(f":x: 損切発注失敗: {res}")
                        else:
                            notify_slack(":x: 発注関数が見つかりません。")
                    except Exception as e:
                        notify_slack(f":x: 損切APIエラー: {e}")
                if not closed:
                    still_open.append(p)
            state["positions"] = still_open

            # 残高/マージン
            usdt_free, equity = get_free_and_equity()
            mr = est_margin_ratio(usdt_free, state["positions"], c)

            if mr < float(getattr(S, "margin_ratio_stop", 0.5)):
                notify_slack(f"🚨 証拠金維持率低下: {mr*100:.1f}% < {float(getattr(S,'margin_ratio_stop',0.5))*100:.0f}% 新規停止")
            elif cond_entry:
                # === EdgeSignal: 票決（OBI/OFI/CVD/清算/ΔOI）で前段フィルター ===
                if EDGE_ENABLED and edge:
                    met = {}  # ← 先に初期化しておく（DBG用に未定義を避ける）
                    try:
                        sig = edge.pick_signal()          # "LONG" / "SHORT" / None
                        edge_votes = 0
                        ofi_z = 0.0
                        try:
                            # 1) edge_votes は last_reasons の "votes=..." から取得
                            if EDGE_ENABLED and edge and getattr(edge, "last_reasons", None):
                                import re
                                joined = " ".join(edge.last_reasons)
                                m_vote = re.search(r"votes=(\d+)", joined)
                                if m_vote:
                                    edge_votes = int(m_vote.group(1))

                            # 2) metrics スナップショットから強フロー指標を取得
                            if EDGE_ENABLED and edge and hasattr(edge, "get_metrics_snapshot"):
                                met = edge.get_metrics_snapshot() or {}
                                # ← 以前 'metrics' を参照していたtypoを修正（metを使う）
                                ofi_z     = float(met.get("ofi_z", 0.0))
                                cons_buy  = int(met.get("cons_buy", 0))
                                cons_sell = int(met.get("cons_sell", 0))
                                cvd_z     = float(met.get("cvd_slope_z", 0.0))
                                # metrics に edge_votes が入っていれば優先
                                edge_votes = int(met.get("edge_votes", edge_votes))
                                # 任意の参照
                                liq_long_usd  = float(met.get("liq_long_usd", 0.0))
                                liq_short_usd = float(met.get("liq_short_usd", 0.0))
                                oi_drop_pct   = float(met.get("oi_drop_pct", 0.0))

                            # --- デバッグ出力（必要な時だけ） ---
                            if bool(getattr(S, "debug_flow", False)):
                                notify_slack(
                                    f"[DBG] OFI z={met.get('ofi_z',0):.2f} | cons={met.get('cons_buy',0)}/{met.get('cons_sell',0)} "
                                    f"| votes={met.get('edge_votes',0)} | ofi_len={met.get('ofi_len',0)}/{met.get('ofi_win',0)} "
                                    f"| trades seen/added={met.get('dbg_trades_seen','?')}/{met.get('dbg_trades_added','?')}"  
                                )
                        except Exception:
                            # 取得失敗時はデフォルト(0)のまま
                            pass
                        reasons = " / ".join(edge.last_reasons)
                        if sig is None:
                            # --- 強フロー例外：regime not ok でも通す ---
                            ofi_th   = float(getattr(S, "regime_override_ofi_z",
                                            getattr(S, "cooldown_override_ofi_z", 2.2)))
                            cons_th  = int(getattr(S, "regime_override_cons",
                                            getattr(S, "cooldown_override_cons", 3)))
                            votes_th = int(getattr(S, "regime_override_votes",
                                            getattr(S, "cooldown_override_votes", 3)))
                            if (abs(ofi_z) >= ofi_th) or (max(cons_buy, cons_sell) >= cons_th) or (int(edge_votes or 0) >= votes_th):
                                sig = "LONG" if ofi_z >= 0 else "SHORT"
                                notify_slack(
                                    f"◯ regime override by strong_flow → {sig} "
                                    f"(OFI z={ofi_z:.2f}, cons={max(cons_buy,cons_sell)}, votes={edge_votes})"
                                )
                                notify_slack(f"🔥 EdgeSignal {sig} | override(strong_flow)")
                            else:
                                # 理由文字列から集計キー
                                reason_txt = " ".join([str(r).lower() for r in reasons])
                                if "regime not ok" in reason_txt:
                                    _bump_skip(state, "regime_not_ok")
                                elif "no consensus" in reason_txt:
                                    _bump_skip(state, "no_consensus")
                                else:
                                    _bump_skip(state, "other")
                                notify_slack(f":インフォメーション: スキップ: EdgeSignal None | {', '.join(reasons)}")
                                last_handled_kline = last_start
                                state['last_kline_start'] = last_start
                                save_state(state)
                                _maybe_send_daily_summary(state)
                                time.sleep(float(S.poll_interval_sec))
                                continue
                        elif sig == "SHORT":
                            notify_slack(f"🔥 EdgeSignal SHORT | {reasons}")
                        else:
                            notify_slack(f"🔥 EdgeSignal LONG | {reasons}")
                    except Exception as e:
                        notify_slack(f"⚠️ EdgeSignal 取得失敗: {e}")
                # ← デバッグ行は try の外で、常に安全に出す
                if EDGE_ENABLED and edge and bool(getattr(S, "debug_flow", False)):
                    try:
                        if not met and hasattr(edge, "get_metrics_snapshot"):
                            met = edge.get_metrics_snapshot() or {}
                        notify_slack(
                            f"[DBG] OFI z={float(met.get('ofi_z',0)):.2f} | "
                            f"cons={int(met.get('cons_buy',0))}/{int(met.get('cons_sell',0))} | "
                            f"votes={int(met.get('edge_votes',0))} | "
                            f"ofi_len={int(met.get('ofi_len',0))}/{int(met.get('ofi_win',0))} | "
                            f"trades seen/added={met.get('dbg_trades_seen','?')}/{met.get('dbg_trades_added','?')}"
                        )
                    except Exception:
                        pass
            
            planned_margin = usdt_free * float(S.position_pct)
            sigmsg = (
                f"Px={c:.4f} SMA10={s10:.4f} SMA50={s50:.4f} "
                f"MACD={m:.4f} Sig={sgn:.4f} RSI={r:.1f} ATR={a:.4f} | PlannedMargin={planned_margin:.4f}"
            )
            if bool(getattr(S, 'debug_flow', False)):
                # ← 直近スナップショットから再取得（0.00表記の回避）
                try:
                    m2 = edge.get_metrics_snapshot() if (EDGE_ENABLED and edge and hasattr(edge, "get_metrics_snapshot")) else {}
                except Exception:
                    m2 = {}
                sigmsg += f" | OFI z={float(m2.get('ofi_z', ofi_z)):.2f} votes={int(m2.get('edge_votes', edge_votes))}"
            notify_slack(f"🧪 シグナル確認: {sigmsg}")

            # === C) 連続エントリー抑制（ATR連動の動的クールダウン + 強フロー解除） ===
            # 1) ATRバッファを更新（stateに保存）
            try:
                atr_buf = state.get("atr_buf") or []
                atr_buf.append(float(a))
                maxlen = int(getattr(S, "cooldown_atr_buf_max", 96))
                if len(atr_buf) > maxlen:
                    atr_buf = atr_buf[-maxlen:]
                state["atr_buf"] = atr_buf
            except Exception:
                pass

            # 2) 短期/長期メディアンを計算
            def _median(xs):
                xs = sorted(xs)
                return xs[len(xs)//2] if xs else float(a)
            short_n = int(getattr(S, "cooldown_atr_short_n", 12))
            long_n  = int(getattr(S, "cooldown_atr_long_n", 48))
            atr_short = _median(atr_buf[-short_n:]) if len(state.get("atr_buf", [])) >= max(4, short_n) else float(a)
            atr_long  = _median(atr_buf[-long_n:])  if len(state.get("atr_buf", [])) >= max(8, long_n)  else atr_short

            # 3) base を比でスケール → クリップ
            base_cd = int(getattr(S, "entry_cooldown_min", 30))
            ratio   = float(atr_short) / max(atr_long, 1e-9)
            dyn_cd  = int(round(base_cd * ratio))
            mn      = int(getattr(S, "cooldown_min_floor", 5))
            mx      = int(getattr(S, "cooldown_max_cap", 30))
            dyn_cd  = max(mn, min(mx, dyn_cd))

            # 4) 強フローならクールダウンを解除（新：一元化＋方向/ADXゲート）
            cooldown_ok = True  # 初期値：前回エントリなしならクールダウン無し
            override_ok, override_note = _cooldown_override_by_flow(edge_obj, S)
            # 票数（edge_votes）による追加解除は必要ならここで足す
            if (not override_ok) and int(edge_votes or 0) >= int(getattr(S, "cooldown_override_votes", 5)):
                override_ok  = True
                override_note = f"{override_note} | votes={int(edge_votes or 0)}"
            # 方向一致（OFIの符号とシグナル方向が合致しないと解除しない）
            if override_ok:
                flow_dir = "LONG" if ofi_z >= 0 else "SHORT"
                if sig and sig != flow_dir:
                    override_ok  = False
                    override_note = f"{override_note} | dir_mismatch({sig} vs {flow_dir})"
            # フラット回避：最低ADX
            if override_ok and float(adx_val or 0.0) < float(getattr(S, "cooldown_override_adx_min", 18.0)):
                override_ok  = False
                override_note = f"{override_note} | adx={float(adx_val or 0.0):.1f}<min"
            if override_ok:
                cooldown_ok = True
                # Slack 注釈へ付加（後段の通知に連結されます）
                relax_note = (relax_note + " | " if relax_note else " | ") + f"CD-override:{override_note}"

            last_entry_iso = state.get("last_entry_time")
            if not strong_flow and last_entry_iso:
                try:
                    last_dt = datetime.fromisoformat(last_entry_iso)
                    cooldown_ok = (datetime.utcnow() - last_dt) >= timedelta(minutes=dyn_cd)
                except Exception:
                    pass

            if strong_flow:
                notify_slack("ℹ️ スキップ解除: flip_cooldown_override（強フロー）")
            elif not cooldown_ok:
                _bump_skip(state, "cooldown")
                notify_slack(f"ℹ️ スキップ: クールダウン中（base={base_cd}→dyn={dyn_cd}, ratio={ratio:.2f} | ATR_med={atr_short:.4f}/{atr_long:.4f})")
                last_handled_kline = last_start
                state['last_kline_start'] = last_start
                save_state(state)
                time.sleep(float(S.poll_interval_sec))
                continue
            # === A) レジーム連動の距離上限（ctx で上書き可） ===
            dist_atr = (c - s10) / max(a, 1e-9)
            trendish = (c > s10 > s50) and (m > sgn) and (r > 60)
            ctx_dist_max = 1.5 if trendish else 0.7
            # 直近レンジHH/LL（range_lookback）を計算
            try:
                look = int(getattr(S, "range_lookback", 60))
                hh = max([rr["high"] for rr in rows_for_calc[-look:]])
                ll = min([rr["low"]  for rr in rows_for_calc[-look:]])
            except Exception:
                hh, ll = h, l
            ctx = {
                "price": c, "atr": a, "sma10": s10, "sma50": s50,
                "rsi": r, "macd": m, "macd_sig": sgn,
                "dist_max_atr": ctx_dist_max,
                "dist_atr": float(dist_atr),
                "edge_votes": int(edge_votes),
                "ofi_z": float(ofi_z),
                "adx": float(adx_val),
                "hh": float(hh), "ll": float(ll),  # ← 追加：レンジ位置用
            }

            if getattr(S, 'use_1h_trend_confirmation', True):
                trend_1h = get_1h_trend(S.symbol)
            else:
                trend_1h = {"trend": "neutral", "sma": 0.0}
            ctx["trend_1h"] = trend_1h

            # Edgeメトリクス（カピチュレーション判定用）を ctx に載せる
            try:
                ctx["liq_long_usd"]  = float(locals().get("liq_long_usd", 0.0))
                ctx["liq_short_usd"] = float(locals().get("liq_short_usd", 0.0))
                ctx["oi_drop_pct"]   = float(locals().get("oi_drop_pct", 0.0))
            except Exception:
                pass
            # Orderflow / Orderbook を取得してガード
            try:
                tdata = fetch_recent_trades_linear(S.symbol, 1000)
                if isinstance(tdata, dict) and "result" in tdata:
                    tlist = [{
                        "side": str(t.get("side") or ("Buy" if str(t.get("isBuyerMaker")) == "False" else "Sell")),
                        "price": float(t["price"]),
                        "qty": float(t.get("size") or t.get("qty") or 0.0),
                        "time": int(t["time"]),
                    } for t in tdata["result"]["list"]]
                else:
                    tlist = tdata
            except Exception as e:
                notify_slack(f":x: Flow取得失敗: {e}")
                tlist = []

            try:
                ob = fetch_orderbook_linear(S.symbol, _DEF_OB_DEPTH)
                if isinstance(ob, dict) and "result" in ob:
                    bids = [(float(p), float(q)) for p, q in ob["result"].get("b", [])]
                    asks = [(float(p), float(q)) for p, q in ob["result"].get("a", [])]
                    book = {"bids": bids, "asks": asks}
                else:
                    book = ob
            except Exception as e:
                notify_slack(f":x: Orderbook取得失敗: {e}")
                book = {"bids": [], "asks": []}
                
            # ---- OB-persist（直近Nサンプルの ask/bid 平均）を更新 → ctxへ ----
            try:
                ob_ratio = _compute_ask_bid_ratio(book, _DEF_OB_DEPTH)
                state["ob_hist"].append(float(ob_ratio))
                maxlen = int(getattr(S, "ob_hist_len", 6))
                if len(state["ob_hist"]) > maxlen:
                    state["ob_hist"] = state["ob_hist"][-maxlen:]
                ob_persist = sum(state["ob_hist"]) / max(1, len(state["ob_hist"]))
            except Exception:
                ob_persist = 1.0
            ctx["ob_persist"] = float(ob_persist)

            ctx.update({
                "edge_votes": int(edge_votes),
                "ofi_z": float(ofi_z),
            })

            # --- エントリーガード判定（必ず ok/why を定義する）---
            ok: bool = False
            why: str = "guard not evaluated"

            # EdgeSignal に応じて LONG/SHORT を選択
            side_for_entry = "LONG"
            try:
                if sig == "SHORT" and getattr(S, "allow_shorts", True):
                    ok, why = decide_entry_guard_short(tlist, book, ctx, S)
                    side_for_entry = "SHORT"
                    notify_slack(f":triangular_ruler: Regime={ctx.get('regime','unknown')} | SHORT guard → {why or 'OK'}")
                else:
                    ok, why = decide_entry_guard_long(tlist, book, ctx, S)
                    side_for_entry = "LONG"
                    notify_slack(f":triangular_ruler: Regime={ctx.get('regime','unknown')} | LONG guard → {why or 'OK'}")
            except Exception as e:
                why = f"guard-eval exception: {e!s}"
                notify_slack(f":x: 例外: {why}")

                        # 強化されたエントリー条件チェック
            enhanced_ok, enhanced_reason = check_enhanced_entry_conditions(ctx, ind, S)
            if not enhanced_ok:
                _bump_skip(state, "regime_not_ok")
                notify_slack(f"ℹ️ スキップ: {enhanced_reason}")
                last_handled_kline = last_start
                state['last_kline_start'] = last_start
                save_state(state)
                time.sleep(float(S.poll_interval_sec))
                continue

            # レジーム別取引制限
            regime = classify_regime(ctx)
            if regime == "neutral":
                # ニュートラルレジームでの取引頻度制限
                neutral_trade_count = state.get("neutral_trade_count", 0)
                if neutral_trade_count >= 2:  # 1時間あたり2回まで
                    _bump_skip(state, "regime_not_ok")
                    notify_slack("ℹ️ スキップ: ニュートラルレジーム取引制限(1時間2回)")
                    last_handled_kline = last_start
                    state['last_kline_start'] = last_start
                    save_state(state)
                    time.sleep(float(S.poll_interval_sec))
                    continue

            # === ここにレジーム別戦略最適化のコードを追加 ===
            regime = classify_regime(ctx)    

            # レジーム別戦略最適化
            if regime == "range":
                # レンジ戦略: 上限でSHORT、下限でLONGに集中
                if side_for_entry == "LONG" and not is_range_lower(ctx):
                    _bump_skip(state, "regime_not_ok")
                    notify_slack(f"ℹ️ スキップ: レンジ下限以外でのLONG禁止 | 現在位置: {((ctx.get('price',0)-ctx.get('ll',0))/(ctx.get('hh',1)-ctx.get('ll',1))*100 if ctx.get('hh',0)>ctx.get('ll',0) else 0):.1f}%")
                    last_handled_kline = last_start
                    state['last_kline_start'] = last_start
                    save_state(state)
                    time.sleep(float(S.poll_interval_sec))
                    continue
                elif side_for_entry == "SHORT" and not is_range_upper(ctx):
                    _bump_skip(state, "regime_not_ok")
                    notify_slack(f"ℹ️ スキップ: レンジ上限以外でのSHORT禁止 | 現在位置: {((ctx.get('price',0)-ctx.get('ll',0))/(ctx.get('hh',1)-ctx.get('ll',1))*100 if ctx.get('hh',0)>ctx.get('ll',0) else 0):.1f}%")
                    last_handled_kline = last_start
                    state['last_kline_start'] = last_start
                    save_state(state)
                    time.sleep(float(S.poll_interval_sec))
                    continue

            elif regime == "trend_strong_long":
                # 強い上昇トレンド: LONGのみ許可
                if side_for_entry == "SHORT":
                    _bump_skip(state, "regime_not_ok")
                    notify_slack("ℹ️ スキップ: 強い上昇トレンド中のSHORT禁止")
                    last_handled_kline = last_start
                    state['last_kline_start'] = last_start
                    save_state(state)
                    time.sleep(float(S.poll_interval_sec))
                    continue


            elif regime == "trend_strong_short":
                # 強い下降トレンド: LONG禁止 ← この処理を追加
                if side_for_entry == "LONG":
                    _bump_skip(state, "regime_not_ok")
                    notify_slack("ℹ️ スキップ: 強い下降トレンド中のLONG禁止")
                    last_handled_kline = last_start
                    state['last_kline_start'] = last_start
                    save_state(state)
                    time.sleep(float(S.poll_interval_sec))
                    continue

            elif regime == "neutral":
                # ニュートラル: 取引頻度50%削減（クールダウン延長で実現）
                current_cd = int(getattr(S, "entry_cooldown_min", 6))
                extended_cd = current_cd * 2  # クールダウン2倍
                # 動的クールダウン計算で既に適用されるので注記のみ
                relax_note = f" | neutral_cd_x2={extended_cd}min"

            # ---- 反対方向エントリー禁止 + 強制フリップ対応ガード ----
            g_res = _guard_opposite_entry(side_for_entry, state)
            _ok_guard, _why_guard, _overrides = _normalize_guard_result(g_res)
            if not _ok_guard:
                _bump_skip(state, "opposite_guard")
                notify_slack(f"ℹ️ 条件成立→スキップ: {_why_guard}")
                last_handled_kline = last_start
                state["last_kline_start"] = last_start
                save_state(state)
                time.sleep(float(S.poll_interval_sec))
                continue

            # --- 発注可否・数量計算 ---
            if len(state["positions"]) >= int(S.max_positions):
                _bump_skip(state, "max_positions")
                notify_slack(f"ℹ️ 条件成立→スキップ: 同時ポジ上限 {len(state['positions'])}/{int(S.max_positions)}")
                last_handled_kline = last_start
                state["last_kline_start"] = last_start
                save_state(state)
                time.sleep(float(S.poll_interval_sec))
                continue
            else:
                margin = usdt_free * float(S.position_pct)
                qty = (margin * float(S.leverage)) / c

                # ---- ATR×係数で初期SL/TPを決定（RR一定） ----
                regime = classify_regime(ctx)            # "trend_up" / "neutral" / "range"
                side   = side_for_entry                  # "LONG" or "SHORT"

                prof = _decide_tp_sl_profile(regime, side, edge_votes, ofi_z, S)
                sl_k  = float(prof["sl_k"])
                tp_rr = float(prof["tp_rr"])

                min_sl = float(getattr(S, "min_sl_usd", 0.20))
                sl_dist = max(sl_k * a, min_sl)

                if side == "LONG":
                    sl_price = c - sl_dist
                    tp_price = c + tp_rr * sl_dist
                else:
                    sl_price = c + sl_dist
                    tp_price = c - tp_rr * sl_dist

            # --- チェイス中はSLとサイズを上書き ---
            if ctx.get("mode") == "chase":
                sl_dist = max(float(getattr(S, "breakout_sl_k", 1.6)) * a, float(getattr(S, "min_sl_usd", 0.20)))
                if side == "LONG":
                    sl_price = c - sl_dist
                    tp_price = c + float(getattr(S, "tp_rr", 1.8)) * sl_dist
                else:
                    sl_price = c + sl_dist
                    tp_price = c - float(getattr(S, "tp_rr", 1.8)) * sl_dist

            # --- 枚数計算の直前で（パッチ1の qty 計算のさらに一行）---
            size_mult = float(getattr(S, "breakout_half_size", 0.5)) if ctx.get("mode") == "chase" else 1.0
            qty *= size_mult

            # ←←← 2段階FLIPを行う場合は「上乗せフリップ」を使わない
            two_stage_flip = bool(_overrides.get("force_flip")) and bool(getattr(S, "allow_atomic_flip", False))
            if not two_stage_flip:
                qty, _flip_note = _apply_flip_overrides_if_any(side_for_entry, qty, _overrides)
                if _flip_note:
                    relax_note = (relax_note + " | " if relax_note else " | 緩和=") + _flip_note
            else:
                _flip_note = "FLIP two-stage"
                relax_note = (relax_note + " | " if relax_note else " | 緩和=") + _flip_note

            # Optional: PostOnly 指値
            placed_postonly = False
            try:
                if getattr(S, "use_postonly_entries", False) and _place_postonly_fn:
                    if _cancel_all_fn:
                        try:
                            _cancel_all_fn(S.symbol)
                        except Exception:
                            pass
                    pull = float(getattr(S, "entry_pullback_atr", 0.25)) * a
                    if side == "LONG":
                        limit_px = min(c, s10 + pull)
                        open_side = "Buy"
                    else:
                        limit_px = max(c, s10 - pull)
                        open_side = "Sell"
                    res = _place_postonly_fn(S.symbol, open_side, qty, limit_px)
                    if isinstance(res, dict) and res.get("retCode") == 0:
                        placed_postonly = True
                        notify_slack(f"🧱 PostOnly指値: {limit_px:.4f} | Qty {qty:.4f}")
                    else:
                        notify_slack(f":x: PostOnly発注失敗: {res}")
            except Exception as e:
                notify_slack(f":x: PostOnly APIエラー: {e}")

            if not placed_postonly:
                if _place_linear_fn:
                    notional = qty * c
                    buy_fee = notional * float(getattr(S, "taker_fee_rate", 0.0007))
                    # --- 最小 Notional チェック ---
                    min_notional = float(getattr(S, "min_notional_usdt", 0.0))
                    if notional < min_notional:
                        _bump_skip(state, "min_notional")
                        notify_slack(f"ℹ️ スキップ: 最小Notional不足 {notional:.2f} < {min_notional:.2f}")
                        last_handled_kline = last_start
                        state['last_kline_start'] = last_start
                        save_state(state)
                        time.sleep(float(S.poll_interval_sec))
                        continue
                    relax_note = locals().get("relax_note", "")  # ← 保険：どの分岐でも値があるように
                    try:
                        open_side = "Buy" if side == "LONG" else "Sell"

                        # --- FLIP Step1: まずは reduce-only で既存ネット玉を完全クローズ ---
                        if two_stage_flip:
                            close_from = str(_overrides.get("flip_from","")).upper()  # "LONG" or "SHORT"
                            close_side = "Sell" if close_from == "LONG" else "Buy"
                            close_qty  = float(_overrides.get("flip_additional_qty", 0.0))
                            if close_qty > 0:
                                res_close = _place_linear_fn(S.symbol, close_side, close_qty, True)  # reduce_only=True
                                if not (isinstance(res_close, dict) and res_close.get("retCode") == 0):
                                    notify_slack(f":x: FLIP Step1 失敗: {res_close}")
                                    # 安全のため Step2 を実行しない
                                    last_handled_kline = last_start
                                    state['last_kline_start'] = last_start
                                    save_state(state)
                                    time.sleep(float(S.poll_interval_sec))
                                    continue
                                notify_slack(f"🔁 FLIP Step1: reduce-only {close_side} qty={close_qty:.4f}")
                                time.sleep(0.3)  # 軽い待機（約定反映の余裕）

                        # --- FLIP Step2（または通常エントリー） ---
                        res = _place_linear_fn(S.symbol, open_side, qty)
                        if isinstance(res, dict) and res.get("retCode") == 0:
                            pos = {
                                "side": "long" if side == "LONG" else "short",
                                "entry_price": c,
                                "qty": qty,
                                "buy_fee": buy_fee,
                                "tp_price": tp_price,
                                "sl_price": sl_price,
                                "time": datetime.utcnow().isoformat(),
                                "be_k":  float(prof.get("be_k", 0.0)),   # 0 or None なら建値移動しない
                                "trail_k": float(prof.get("trail_k", 0.0)), # >0 ならトレール有効
                                "profile": str(prof.get("name","")),
                                "flip": bool(_overrides.get("force_flip", False)),
                                "risk_sl_dist": abs(c - sl_price),
                            }
                            # === 現在のレジームを状態に保存 ===
                            state["last_regime"] = regime

                            state["positions"].append(pos)
                            state["last_entry_time"] = datetime.utcnow().isoformat()
                            # flip時はローカルの反対玉を掃除（net混在で以後ブロックするのを防ぐ）
                            if _overrides.get("force_flip"):
                                _cleanup_positions_after_flip(side, state)
                                state["last_flip_time"] = datetime.utcnow().isoformat()
                                try:
                                    notify_slack(f"🔁 FLIP 実行: {_overrides.get('flip_from','?')}→{_overrides.get('flip_to','?')}")
                                except Exception:
                                    pass                              
                            _on_new_entry(state, is_flip=bool(_overrides.get("force_flip")) if '_overrides' in locals() else False)
                            state["last_entry_time"] = datetime.utcnow().isoformat()  # C) クールダウン開始
                            relax_note = locals().get("relax_note", "")
                            prof_name = str(prof.get("name",""))
                            notify_slack(
                                f"💰 エントリー({side}): {c:.4f} | TP {tp_price:.4f} | SL {sl_price:.4f} | "
                                f"Qty {qty:.4f} | 使用証拠金~{margin:.2f}USDT | 管理={prof_name}{relax_note}"
                            )
                        else:
                            notify_slack(f":x: 発注失敗: {res}")
                    except Exception as e:
                        notify_slack(f":x: 発注APIエラー: {e}")
                elif _place_simple_fn:
                    relax_note = locals().get("relax_note", "")  # ← 保険：どの分岐でも値があるように
                    try:
                        side_simple = "Buy" if side == "LONG" else "Sell"
                        res = _place_simple_fn(side_simple, qty, c, tp_price)
                        ok_simple = False
                        if isinstance(res, dict):
                            ok_simple = (res.get("retCode") == 0) or str(res.get("retMsg", "")).lower().startswith("order")
                        if ok_simple:
                            pos = {
                                "side": "long" if side == "LONG" else "short",
                                "entry_price": c,
                                "qty": qty,
                                "buy_fee": 0.0,
                                "tp_price": tp_price,
                                "sl_price": sl_price,
                                "time": datetime.utcnow().isoformat(),
                                "be_k":  float(prof.get("be_k", 0.0)),
                                "trail_k": float(prof.get("trail_k", 0.0)),
                                "profile": str(prof.get("name","")),
                            }
                            _on_new_entry(state, is_flip=bool(_overrides.get("force_flip")) if '_overrides' in locals() else False)
                            state["positions"].append(pos)
                            state["last_entry_time"] = datetime.utcnow().isoformat()
                            relax_note = locals().get("relax_note", "")
                            prof_name = str(prof.get("name",""))
                            notify_slack(
                                f"💰 エントリー({side}): {c:.4f} | TP {tp_price:.4f} | SL {sl_price:.4f} | "
                                f"Qty {qty:.4f} | 使用証拠金~{margin:.2f}USDT | 管理={prof_name}{relax_note}"
                            )
                        else:
                            notify_slack(f":x: 発注失敗: {res}")
                    except Exception as e:
                        notify_slack(f":x: シンプル発注APIエラー: {e}")
                else:
                    notify_slack(":x: 発注関数が見つかりません。bybit.py を確認してください。")

            last_handled_kline = last_start
            state["last_kline_start"] = last_start
            save_state(state)
            _maybe_send_daily_summary(state)
            time.sleep(float(S.poll_interval_sec))

        except KeyboardInterrupt:
            print("停止要求。終了します。")
            break
        except Exception as e:
            print(f"[EXCEPTION] {e}")
            traceback.print_exc()
            try:
                notify_slack(f":x: 例外: {e}")
            except Exception:
                pass
            time.sleep(max(5.0, float(S.poll_interval_sec)))

if __name__ == "__main__":
    run_loop()
