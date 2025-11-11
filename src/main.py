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
from pathlib import Path
from . import slack as _slk  # 既存の from .slack import notify_slack があってもOK（後で上書きします）


# .env 読み込み
from .env import load_env
load_env()

from .config import STRATEGY as S, API
from .indicators import rsi, macd, atr, sma
from .slack import notify_slack, _flush_slack_queue

from edge_signal_pack.indicators import adx as ws_adx
from edge_signal_pack.signal_engine import EdgeSignalEngine
EDGE_ENABLED = True
edge = None

# Exit Engine 読み込み（存在しなくても起動可）
try:
    from .exit_engine import evaluate as _exit_evaluate
except Exception:
    _exit_evaluate = None

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

from .micro_entry import compute_pullback_target, wait_for_micro_entry

# ===== bybit.py の関数名差異に自動対応（get_klines_linearが無くてもOK）=====
from typing import Optional
try:
    from .import bybit as _bx_loaded
    _bx: Optional[ModuleType] = _bx_loaded
except Exception:
    _bx = None  

_DEF_OB_DEPTH = getattr(S, "ob_depth", 50)

try:
    if bool(getattr(S, "debug_boot", False)):
        notify_slack(f"[DEBUG] using bybit module: {getattr(_bx, '__file__', 'N/A')}")
        notify_slack(f"[DEBUG] has place_linear_market_order? {hasattr(_bx, 'place_linear_market_order') if _bx else False}")
except Exception:
    pass

# ===== 日次テキストロガー & Slackフィルタ ================================
from zoneinfo import ZoneInfo  # 既にimport済みなら重複OK

class _DailyTextLogger:
    """
    ・JST日付ごとのテキストファイル（./logs/YYYY-MM-DD.txt）に追記
    ・“1本の足で発生するログ束”をバッファし、終端イベントでまとめて書き出す
    """
    def __init__(self, tz: str = "Asia/Tokyo"):
        self.tz = ZoneInfo(tz)
        self.bundle_key = None          # 例: 足の start(ms)
        self.bundle_lines: list[str] = []
        self.base_dir = Path("logs")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _jst_now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).astimezone(self.tz)

    def _date_key(self) -> str:
        return self._jst_now().strftime("%Y-%m-%d")

    def _file_path(self) -> Path:
        return self.base_dir / f"{self._date_key()}.txt"

    def start_bundle(self, key):
        # 直前の束が残っていれば一度吐き出してから開始
        if self.bundle_lines:
            self.flush(force=True)
        self.bundle_key = key
        self.bundle_lines = []

    def add_line(self, text: str):
        if text is None:
            return
        self.bundle_lines.append(str(text).rstrip("\n"))

    def _is_terminal(self, text: str) -> bool:
        """束を締める合図となる行かどうか"""
        if not text:
            return False
        t = str(text).strip()
        # スキップ（各種）
        if t.startswith("ℹ️ スキップ") or t.startswith(":インフォメーション: スキップ"):
            return True
        # エントリー（PostOnly経由も最終的にはここへ）
        if t.startswith("💰 エントリー"):
            return True
        # PostOnly未充足 → 監視移行（その足の束は締めてよい）
        if ("PostOnly未充足" in t) or ("監視に移行" in t):
            return True
        # 発注失敗/APIエラーなど、その足の決着がつく系
        if t.startswith(":x:"):
            return True
        return False

    def flush(self, force: bool = False):
        """現在の束をファイルへ出力（force=True か 終端イベント時）"""
        if not self.bundle_lines:
            return
        path = self._file_path()
        ts = self._jst_now().strftime("%H:%M:%S")
        header = f"--- [{ts}] bundle key={self.bundle_key} ---"
        with path.open("a", encoding="utf-8") as f:
            f.write(header + "\n")
            for ln in self.bundle_lines:
                f.write(ln + "\n")
            f.write("\n")
        # 次の束に備えてクリア
        self.bundle_lines = []
        self.bundle_key = None

# グローバルなロガー（S.timezone が無ければ Asia/Tokyo）
_TEXTLOG = _DailyTextLogger(S.timezone if hasattr(S, "timezone") else "Asia/Tokyo")

def _should_send_to_slack(text: str) -> bool:
    """Slackへ送るのは『エントリー/利確/損切』＋（任意で）起動系"""
    if not text:
        return False
    t = str(text).strip()

    # 成果通知（必ずSlackへ）
    if (
        t.startswith("💰 エントリー")
        or t.startswith("✅ 利確")
        or t.startswith("🛑 損切")
    ):
        return True

    # --- 起動系はオプションでSlackへ（既定: True）---
    if getattr(S, "slack_boot_notify", True):
        if (
            t.startswith("🟢 起動")
            or t.startswith("🚀 起動ステータス")
            or t.startswith("👀 監視開始")
            or ("EdgeSignalEngine 起動" in t)
        ):
            return True

def notify_slack(text: str, **kwargs) -> None:
    """
    中央集約ラッパ：
      1) ログ束に追加
      2) 終端なら束をファイルへフラッシュ
      3) Slackは成果のみ送る（エントリー/利確/損切）
    """
    try:
        # 1) 束へ追加（“束”が未開始の場面でも、まずは束に入れる）
        _TEXTLOG.add_line(text)
        # 2) 終端判定 → ファイルへ吐き出す
        if _TEXTLOG._is_terminal(text):
            _TEXTLOG.flush(force=True)
        # 3) Slackへは必要最小限だけ
        if _should_send_to_slack(text):
            _slk.notify_slack(text, **(kwargs or {}))
    except Exception:
        # 例外時は安全側でSlackだけでも送っておく
        if _should_send_to_slack(text):
            try:
                _slk.notify_slack(text, **(kwargs or {}))
            except Exception:
                pass


_LOG_ONCE = {}
def _log_once(key: str, msg: str, interval_sec: float = 60.0):
    now = time.time()
    last = _LOG_ONCE.get(key, 0.0)
    if now - last >= interval_sec:
        _LOG_ONCE[key] = now
        notify_slack(msg)

# --- VWMA（出来高加重移動平均）をローカル実装 ---------------------------------
def _vwma(prices: list[float], volumes: list[float], length: int) -> list[float]:
    n = int(length)
    if n <= 0:
        return [0.0 for _ in prices]
    out: list[float] = []
    acc_pv = 0.0
    acc_v = 0.0
    q = [] # 窓: (p*v, v)
    for i, (p, v) in enumerate(zip(prices, volumes)):
        pv = float(p) * float(v)
        q.append((pv, float(v)))
        acc_pv += pv
        acc_v += float(v)
        if len(q) > n:
            old_pv, old_v = q.pop(0)
            acc_pv -= old_pv
            acc_v -= old_v
        out.append((acc_pv / acc_v) if acc_v > 0 else float(p))
    return out

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
def _decide_tp_sl_profile(regime: str, side: str, votes: int, ofi_z: float, ctx: dict | None = None, S=S) -> dict:
    """
    レジーム/フローに応じて TP/SL 管理プロファイルを決定。
    返り値例: {"name":"trend_strong_long", "sl_k":1.2, "tp_rr":2.0, "be_k":0.6}
              {"name":"range", "sl_k":0.7, "tp_rr":1.0, "trail_k":0.5}
    """
    # “強トレンド合致”の判定
    # 票数＋OFI z に加え、MTF整合/強さスコアでも強トレンド扱いにする
    need_votes   = int(getattr(S, "trend_votes_min", 2))
    need_ofi_z   = float(getattr(S, "trend_ofi_z_min", 1.5))
    score_min    = int(getattr(S, "strong_score_min", 4))
    # ctx は任意（None可）→ 無ければ MTF/スコアは不使用扱い
    _ctx         = ctx or {}
    mtf_align    = _ctx.get("mtf_align", "none")
    score_up     = int(_ctx.get("strong_score_up", 0))
    score_down   = int(_ctx.get("strong_score_down", 0))
    aligned_flow = (votes >= need_votes) and ((ofi_z >=  need_ofi_z) if side == "LONG" else (ofi_z <= -need_ofi_z))
    aligned_mtf  = (mtf_align == ("up" if side == "LONG" else "down")) if ctx else False
    aligned_scr  = ((score_up >= score_min) if side == "LONG" else (score_down >= score_min)) if ctx else False
    aligned      = aligned_flow or aligned_mtf or aligned_scr

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

    hits = 0
    if abs(ofi_z) >= th_ofi: hits += 1
    if (ofi_z >= 0 and cons_buy >= th_cons) or (ofi_z < 0 and cons_sell >= th_cons): hits += 1
    if int(edge_votes or 0) >= th_votes: hits += 1
    strong = hits >= int(getattr(S, "regime_override_min_triggers", 2))
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
                # Bybit v5 kline: [start, open, high, low, close, volume, turnover, ...]
                vol = float(it[5]) if len(it) > 5 and it[5] is not None else 0.0
                tov = float(it[6]) if len(it) > 6 and it[6] is not None else 0.0
                rows.append({
                    "start": start_ts,
                    "open": float(it[1]),
                    "high": float(it[2]),
                    "low": float(it[3]),
                    "close": float(it[4]),
                    "volume": vol,
                    "turnover": tov,
                })
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
                adapted.append({
                    "start": ts,
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r.get("volume") or r.get("vol") or 0.0),
                    "turnover": float(r.get("turnover") or 0.0),
                })
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
    vols = [float(r.get("volume", 0.0)) for r in rows]
    rsi_vals = rsi(closes, int(getattr(S, "rsi_period", 14)))
    macd_line, signal_line, _ = macd(closes,
                                     int(getattr(S, "macd_fast", 12)),
                                     int(getattr(S, "macd_slow", 26)),
                                     int(getattr(S, "macd_signal", 9)))
    macd_hist = [float(m) - float(s) for m, s in zip(macd_line, signal_line)]
    atr_vals = atr(highs, lows, closes, int(getattr(S, "atr_period", 14)))
    sma10 = sma(closes, 10)
    sma50 = sma(closes, 50)
    # VWMA（出来高加重MA）
    vw_fast_len = int(getattr(S, "vwma_fast_len", 20))
    vw_slow_len = int(getattr(S, "vwma_slow_len", 50))
    vwma_fast = _vwma(closes, vols, vw_fast_len)
    vwma_slow = _vwma(closes, vols, vw_slow_len)
    # 出来高MA（ボリューム拡張検出用）
    vol_ma_len = int(getattr(S, "volume_ma_len", 20))
    vol_ma = sma(vols, vol_ma_len)
    return {
        "rsi": rsi_vals,
        "macd": macd_line,
        "signal": signal_line,
        "macd_hist": macd_hist,
        "atr": atr_vals,
        "sma10": sma10,
        "sma50": sma50,
        "vwma_fast": vwma_fast,
        "vwma_slow": vwma_slow,
        "volume": vols,
        "vol_ma": vol_ma,
        "close": closes, "high": highs, "low": lows,
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
    state = load_state() or {}
    state.setdefault("watch_orders", [])
    state.setdefault("_last_sync", 0.0)  # 追加: 同期タイムスタンプの初期化
    state.setdefault("sl_grace", {})     # ExitEngine: SL猶予マップ
    state.setdefault("exit_engine", {})  # ExitEngine: 内部統計

    
    # === ニュートラル取引カウントのリセット処理 ===
    # ===== 追加①: bybit関数の参照を上の初期化ブロックに追記 =====
    _get_order_rt_fn   = getattr(_bx, "get_order_realtime", None) if _bx else None
    _get_execs_fn      = getattr(_bx, "get_executions_by_order", None) if _bx else None
    _cancel_order_fn   = getattr(_bx, "cancel_order", None) if _bx else None
        # --- PostOnlyキャンセル検証 / 部分約定取り込み / 取引所との整合ウォッチ ---
    def _order_status_local(oid: str) -> tuple[str, float, float]:
        """(status, cumExecQty, avgPrice) を返す。失敗時は空/0."""
        st, filled, avg = "", 0.0, 0.0
        if not oid or not _get_order_rt_fn:
            return st, filled, avg
        try:
            od = _get_order_rt_fn(S.symbol, oid)
            items = (od.get("result") or {}).get("list") or []
            o = items[0] if items else {}
            st = str(o.get("orderStatus") or o.get("status") or "")
            filled = float(o.get("cumExecQty") or o.get("cumQty") or 0.0)
            avg = float(o.get("avgPrice") or 0.0)
        except Exception:
            pass
        return st, filled, avg

    def _adopt_position_from_fill(side: str, sz: float, avg_px: float,
                                  tp_price: float, sl_price: float,
                                  prof: dict, overrides: dict | None):
        """キャンセル直後/監視中に検知した実約定をローカルstateへ反映"""
        if sz <= 0:
            return
        fee_rate = float(getattr(S, "maker_fee_rate",
                         getattr(S, "taker_fee_rate", 0.0007)))
        notional = float(sz) * float(avg_px or 0.0)
        buy_fee  = notional * fee_rate
        pos = {
            "side": "long" if side == "LONG" else "short",
            "entry_price": float(avg_px or 0.0),
            "qty": float(sz),
            "buy_fee": float(buy_fee),
            "tp_price": float(tp_price),
            "sl_price": float(sl_price),
            "time": datetime.utcnow().isoformat(),
            "be_k":  float((prof or {}).get("be_k", 0.0)),
            "trail_k": float((prof or {}).get("trail_k", 0.0)),
            "profile": str((prof or {}).get("name","")),
            "flip": bool((overrides or {}).get("force_flip", False)),
            "risk_sl_dist": abs(float(avg_px or 0.0) - float(sl_price)),
        }
        state["positions"].append(pos)
        state["last_entry_time"] = datetime.utcnow().isoformat()
        save_state(state)  # 追加: すぐ永続化（途中で continue してもポジション喪失しない）
        notify_slack(
            f"💰 エントリー({side})[キャンセル後の実充足検知]: "
            f"{(avg_px or 0.0):.4f} | TP {tp_price:.4f} | SL {sl_price:.4f} | Qty {sz:.4f}"
        )

    def _watchdog_open_orders():
        """キャンセルしたはずの注文を継続監視し、約定→state反映 / 完全キャンセルを確認する"""
        wlist = list(state.get("watch_orders") or [])
        if not wlist:
            return
        new_w = []
        for w in wlist:
            oid = w.get("oid")
            st, fq, ap = _order_status_local(oid)
            if fq and fq > 0.0:
                _adopt_position_from_fill(
                    w.get("side","LONG"),
                    float(fq),
                    float(ap or 0.0) or float(w.get("last_price", 0.0) or 0.0),
                    float(w.get("tp")),
                    float(w.get("sl")),
                    w.get("prof") or {},
                    w.get("overrides") or {},
                )
                continue  # 取り込み完了 → 監視から除外
            if st and st.lower().startswith("cancel"):
                continue  # 完全キャンセル確認 → 除外
            # 監視継続（TTL超過で最終キャンセル再試行）
            if time.time() - float(w.get("_created", time.time())) > float(getattr(S, "postonly_watchdog_ttl_sec", 600)):
                if _cancel_order_fn and oid:
                    try:
                        _cancel_order_fn(S.symbol, oid)
                    except Exception:
                        pass
                continue
            new_w.append(w)
        if new_w != wlist:
            state["watch_orders"] = new_w
            save_state(state)

    def _reconcile_with_exchange(current_price: float):
        """定期的に取引所のネット玉とローカルstateを照合し、乖離時に対処"""
        if not _get_positions_fn:
            return
        # ローカルのネット数量
        q_local = 0.0
        for p in state.get("positions", []):
            q = float(p.get("qty", 0))
            q_local += q if (p.get("side","").lower() == "long") else -q
        # 取引所のネット数量と平均価格
        try:
            res = _get_positions_fn(S.symbol)
        except Exception:
            return
        items = []
        if isinstance(res, dict):
            r = res.get("result") or res.get("data") or res
            items = r.get("list") or r.get("positions") or r.get("data") or []
        elif isinstance(res, list):
            items = res
        q_ex, px_sum, q_sum = 0.0, 0.0, 0.0
        for it in items:
            q = it.get("size") or it.get("qty") or it.get("positionQty")
            q = float(q or 0.0)
            if abs(q) <= 0:
                continue
            side = (it.get("side") or it.get("positionSide") or "").lower()
            ep = float(it.get("avgPrice") or it.get("entryPrice") or 0.0)
            if side in ("buy","long"):
                q_ex += q
            elif side in ("sell","short"):
                q_ex -= q
            else:
                q_ex += q if q > 0 else -q
            if ep > 0:
                px_sum += ep * q
                q_sum  += q
        avg_px_ex = (px_sum / q_sum) if q_sum > 0 else 0.0

        tol = float(getattr(S, "sync_tolerance_qty", 1e-6))
        if abs(q_ex - q_local) <= tol:
            return  # 整合

        # 乖離対処：①自動クローズ（希望時） ②ローカルへ取り込み
        if bool(getattr(S, "auto_flatten_on_desync", False)) and abs(q_ex) > 0:
            try:
                close_side = "Sell" if q_ex > 0 else "Buy"
                q_to_close = abs(q_ex)
                if _place_linear_fn:
                    res = _place_linear_fn(S.symbol, close_side, q_to_close, True)
                    notify_slack(f"🚨 自動解消(desync): {close_side} {q_to_close:.4f} reduce-only | ret={res}")
            except Exception as e:
                notify_slack(f":x: 自動解消失敗: {e}")
        else:
            side = "LONG" if q_ex > 0 else "SHORT"
            # 取り込み時のTP/SLは現在のATR/プロファイルで安全側に再設定
            prof = _decide_tp_sl_profile("neutral", side, 0, 0.0, None, S)
            atr_v = float(state.get("atr_buf", [0.0])[-1] if state.get("atr_buf") else 0.0)
            sl_k  = float(prof.get("sl_k", 1.0))
            sl_d  = max(sl_k * atr_v, float(getattr(S, "min_sl_usd", 0.20)))
            base  = avg_px_ex or current_price
            if side == "LONG":
                sl = base - sl_d
                tp = base + float(prof.get("tp_rr", 1.5)) * sl_d
            else:
                sl = base + sl_d
                tp = base - float(prof.get("tp_rr", 1.5)) * sl_d
            _adopt_position_from_fill(side, abs(q_ex), base, tp, sl, prof, {})
            notify_slack("⚠️ 取引所≠ローカルの不整合を検知 → ローカルに反映しました")
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

    # 起動メッセージを即時に出す（レート制限キューに乗ってもここで吐き出す）
    send_startup_status(state)
    _flush_slack_queue()
    notify_slack("✅ 監視開始（確定足待ち）")
    _flush_slack_queue()

    # ---- 監視/整合チェックをまとめたハウスキーピング ----
    def _housekeep_sync(c_hint: float | None = None):
        # PostOnly監視（キャンセル済みのはずの注文が後から約定していないか）
        _flush_slack_queue()  # ← これを追加
        try:
            _watchdog_open_orders()
        except Exception:
            pass
        # Bybit実在ポジションとローカルstateの整合を一定間隔で同期
        try:
            if time.time() - float(state.get("_last_sync", 0.0)) > float(getattr(S, "sync_interval_sec", 30)):
                price = float(c_hint) if c_hint is not None else float(state.get("last_price", 0.0) or 0.0)
                _reconcile_with_exchange(price)
                state["_last_sync"] = time.time()
                save_state(state)
        except Exception:
            pass

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
            _log_once("edge_start", ":electric_plug: EdgeSignalEngine 起動", 600)
            _flush_slack_queue()
            edge.is_active_hours_jst = lambda: True  # ← 時間帯ふぃるふぃるたー無効化
        except Exception as e:
            notify_slack(f":x: EdgeSignalEngine 初期化失敗: {e}")
            _flush_slack_queue()
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

            # ★確定足待ちの前に、監視だけは毎ループ回す
            try:
                price_hint = float(rows[-1]["close"])
            except Exception:
                price_hint = None
            _housekeep_sync(price_hint)   # 毎ループのPostOnly監視＆取引所同期
            
            closed_idx = get_latest_closed_index(rows, int(S.interval_min))
            if closed_idx is None:
                log_wait_once(rows[-1]["start"])
                time.sleep(float(S.poll_interval_sec))
                continue

            last_start = rows[closed_idx]["start"]

            if last_handled_kline == last_start:
                time.sleep(float(S.poll_interval_sec))
                continue

            _TEXTLOG.start_bundle(last_start)

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
            # 追加: MACDヒスト・VWMA・出来高系
            mh = float((ind.get("macd_hist") or [0.0])[idx])
            mh_p = float((ind.get("macd_hist") or [0.0, 0.0])[idx-1]) if idx > 0 else mh
            vwf = float((ind.get("vwma_fast") or [s10])[idx])
            vws = float((ind.get("vwma_slow") or [s50])[idx])
            vol_n = float((ind.get("volume") or [0.0])[idx])
            vol_m = float((ind.get("vol_ma") or [0.0])[idx])

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
            
            # ==== ExitEngine用の軽量コンテキスト/板・約定を先に準備 ====
            ctx_exit = {
                "price": c, "high": h, "low": l, "atr": a,
                "rsi": r, "macd_hist": mh, "sma10": s10, "sma50": s50,
            }
            # classify_regime が必要とする最低限キーを埋めて regime を決定
            _tmp = {
                "price": c, "atr": a, "sma10": s10, "sma50": s50,
                "rsi": r, "macd": m, "macd_sig": sgn,
                "macd_hist": mh, "macd_hist_prev": mh_p,
                "vwma_fast": vwf, "vwma_slow": vws,
                "volume": vol_n, "vol_ma": vol_m,
                "dist_atr": (c - s10) / max(a, 1e-9),
                "dist_max_atr": 999.0,
            }
            ctx_exit["regime"] = classify_regime(_tmp)

            # Orderflow / Orderbook（ExitEngine用）を1回だけ取得
            try:
                tdata_ex = fetch_recent_trades_linear(S.symbol, 600)
                if isinstance(tdata_ex, dict) and "result" in tdata_ex:
                    tlist_exit = [{
                        "side": str(t.get("side") or ("Buy" if str(t.get("isBuyerMaker")) == "False" else "Sell")),
                        "price": float(t["price"]),
                        "qty": float(t.get("size") or t.get("qty") or 0.0),
                        "time": int(t["time"]),
                    } for t in tdata_ex["result"]["list"]]
                else:
                    tlist_exit = tdata_ex
            except Exception:
                tlist_exit = []

            try:
                ob_ex = fetch_orderbook_linear(S.symbol, _DEF_OB_DEPTH)
                if isinstance(ob_ex, dict) and "result" in ob_ex:
                    bids = [(float(p), float(q)) for p, q in ob_ex["result"].get("b", [])]
                    asks = [(float(p), float(q)) for p, q in ob_ex["result"].get("a", [])]
                    book_exit = {"bids": bids, "asks": asks}
                else:
                    book_exit = ob_ex
            except Exception:
                book_exit = {"bids": [], "asks": []}
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
                # ==== Exit Engine（動的決済） ====
                if _exit_evaluate and bool(getattr(S, "exit_engine_enable", True)):
                    try:
                        ex = _exit_evaluate(p, ctx_exit, book_exit, tlist_exit, edge, state, S, h, l)
                    except Exception as _e:
                        ex = {"action": "HOLD", "reason": f"exit_engine_error:{_e}"}

                    act = (ex or {}).get("action", "HOLD")

                    # --- 1) SL猶予（ヒゲ救済）
                    if act == "SL_GRACE":
                        key = str(p.get("time") or "")
                        state["sl_grace"][key] = time.time() + int(ex.get("grace_sec", 15))
                        save_state(state)
                        _log_once(
                            f"slgrace:{key}",
                            f"🛟 SL猶予 {int(ex.get('grace_sec',15))}s 開始 | {p_side} | 理由: {ex.get('reason','')}",
                            interval_sec=15.0
                        )

                    # --- 2) SL更新（将来拡張用：トレール等）
                    elif act == "UPDATE_SL":
                        try:
                            ns = float(ex.get("new_sl"))
                            if p_side == "long":
                                p["sl_price"] = max(float(p.get("sl_price", ep - 9e9)), ns)
                            else:
                                p["sl_price"] = min(float(p.get("sl_price", ep + 9e9)), ns)
                            _log_once(
                                f"updatesl:{p.get('time','')}",
                                f"🧷 SL更新 → {float(p['sl_price']):.4f} ({p_side})",
                                interval_sec=10.0
                            )
                        except Exception:
                            pass

                    elif act in ("TP_PART", "TP_ALL", "CUT"):
                        if _place_linear_fn:
                            try:
                                close_side = "Sell" if p_side == "long" else "Buy"
                                ratio = 1.0 if act in ("TP_ALL", "CUT") else float(ex.get("ratio", 0.5))
                                qty_all = float(p["qty"])
                                qty_close = max(0.0, min(qty_all, qty_all * ratio))
                                if qty_close > 0:
                                    res = _place_linear_fn(S.symbol, close_side, qty_close, True)
                                    if isinstance(res, dict) and res.get("retCode") == 0:
                                        exit_price = _fill_price_from_res(res, c)  # 無ければ c
                                        exit_notional = qty_close * exit_price
                                        if p_side == "long":
                                            gross = (exit_price - ep) * qty_close
                                        else:
                                            gross = (ep - exit_price) * qty_close
                                        buy_fee_part = float(p.get("buy_fee", 0.0)) * (qty_close / max(qty_all, 1e-9))
                                        sell_fee = exit_notional * float(getattr(S, "taker_fee_rate", 0.0007))
                                        net = gross - buy_fee_part - sell_fee

                                        realized_pnl_log.append(net)
                                        update_trading_state(state, net, net > 0)

                                        # 新シグネチャで呼び出し（RR集計用）
                                        _on_close_trade(
                                            state,
                                            entry=float(ep),
                                            exit_=float(exit_price),
                                            side=str(p_side),  # 'long' / 'short'
                                            risk_sl_dist=float(abs(ep - float(p.get("sl_price", ep)))),
                                            was_flip=bool(p.get("flip", False)),
                                        )

                                        remain = qty_all - qty_close
                                        if remain <= 1e-10:
                                            p["closed"] = True
                                            closed = True
                                            # SL猶予キーを掃除
                                            try:
                                                state.get("sl_grace", {}).pop(str(p.get("time") or ""), None)
                                                save_state(state)
                                            except Exception:
                                                pass
                                            notify_slack(
                                                f"✅ 利確({p_side}, 早期): {net:+.2f} USDT | {ep:.4f}→{exit_price:.4f} | Qty {qty_close:.4f} | {ex.get('reason','')}"
                                            )
                                        else:
                                            # 残玉へ buy_fee を按分して更新（二重控除防止）
                                            p["qty"] = remain
                                            p["buy_fee"] = float(p.get("buy_fee", 0.0)) * (remain / max(qty_all, 1e-9))
                                            notify_slack(
                                                f"✅ 利確({p_side}, 部分): {net:+.2f} USDT | {ep:.4f}→{exit_price:.4f} | Qty {qty_close:.4f} | 残 {remain:.4f} | {ex.get('reason','')}"
                                            )
                                    else:
                                        notify_slack(f":x: 早期決済失敗: {res}")
                            except Exception as e:
                                notify_slack(f":x: 早期決済APIエラー: {e}")
                        # act==CUT でもここで全決済済み

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
                                entry=float(ep),
                                exit_=float(exit_price),
                                side=str(p_side),
                                risk_sl_dist=float(p.get("risk_sl_dist", abs(ep - float(p.get("sl_price", ep))))),
                                was_flip=bool(p.get("flip", False)),
                            )
                            closed = True
                        else:
                            notify_slack(f":x: 決済失敗: {res}")
                    except Exception as e:
                        notify_slack(f":x: 決済APIエラー: {e}")
                # 損切（SLグレース中は保留）
                sl_grace_ok = True
                try:
                    key = str(p.get("time") or "")
                    now_ts = time.time()
                    until = float(state.get("sl_grace", {}).get(key, 0.0))
                    if now_ts < until:
                        sl_grace_ok = False
                        _log_once(
                            f"slgrace_hold:{key}",
                            "🛟 SL猶予中（決済保留）",
                            interval_sec=10.0
                        )
                    elif until > 0:
                        # 猶予は終了しているのでクリーンアップ
                        state["sl_grace"].pop(key, None)
                        save_state(state)
                except Exception:
                    sl_grace_ok = True

                if sl_grace_ok and not closed and (
                    (p_side == "long"  and l <= float(p.get("sl_price", -1))) or
                    (p_side == "short" and h >= float(p.get("sl_price", 1e9)))
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
                        reasons = " / ".join(getattr(edge, "last_reasons", []) or [])
                        if sig is None:
                            # --- 強フロー例外：regime not ok でも通す ---
                            ofi_th   = float(getattr(S, "regime_override_ofi_z",
                                            getattr(S, "cooldown_override_ofi_z", 2.2)))
                            cons_th  = int(getattr(S, "regime_override_cons",
                                            getattr(S, "cooldown_override_cons", 3)))
                            votes_th = int(getattr(S, "regime_override_votes",
                                            getattr(S, "cooldown_override_votes", 3)))
                            same_dir_cons = (ofi_z >= 0 and cons_buy  >= cons_th) or (ofi_z < 0 and cons_sell >= cons_th)
                            if (abs(ofi_z) >= ofi_th) or same_dir_cons or (int(edge_votes or 0) >= votes_th):
                                sig = "LONG" if ofi_z >= 0 else "SHORT"
                                notify_slack(f"🔥 EdgeSignal {sig} (override=strength) | {reasons} | OFI z={ofi_z:.2f} cons={max(cons_buy,cons_sell)} votes={edge_votes}")
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
                        _log_once(
                            "dbg_flow_note",
                            f"[DBG] OFI z={float(met.get('ofi_z',0)):.2f} | "
                            f"cons={int(met.get('cons_buy',0))}/{int(met.get('cons_sell',0))} | "
                            f"votes={int(met.get('edge_votes',0))} | "
                            f"ofi_len={int(met.get('ofi_len',0))}/{int(met.get('ofi_win',0))} | "
                            f"trades seen/added={met.get('dbg_trades_seen','?')}/{met.get('dbg_trades_added','?')}",
                            5.0
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
                "macd_hist": mh, "macd_hist_prev": mh_p,
                "vwma_fast": vwf, "vwma_slow": vws,
                "volume": vol_n, "vol_ma": vol_m,
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
            # Regime を先に決定して ctx へ格納（ログと以降の判定で同一値を使う）
            ctx["regime"] = classify_regime(ctx)
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

            # ガード結果を“必ず”反映（これが無いと NG でも先へ進む）
            if not ok:
                _why = str(why or "")
                # ガード理由が「待ち」（SHORT=戻り売り待ち / LONG=押し目待ち）のときは
                # スキップせずに PostOnly 指値を置く通常フローへ進める
                _guard_wait = (
                    (side_for_entry == "SHORT" and "戻り売り待ち" in _why) or
                    (side_for_entry == "LONG"  and "押し目待ち" in _why)
                )
                if _guard_wait and getattr(S, "use_postonly_entries", True) and _place_postonly_fn:
                    ctx["force_pullback_limit"] = True  # 指値側で引き幅を“待ち”仕様に
                    notify_slack("🧱 ガード=待ち → 指値に切替（PostOnlyで配置します）")
                    ok = True  # このまま通常の発注フローへ
                else:
                    _bump_skip(state, "guard_ng")
                    notify_slack(f"ℹ️ スキップ: エントリーガード不成立 ({why})")
                    last_handled_kline = last_start
                    state['last_kline_start'] = last_start
                    save_state(state)
                    time.sleep(float(S.poll_interval_sec))
                # ← continue は "スキップ" の場合のみ
                if not ok:
                    continue

            # 強化チェック等で方向を参照できるよう明示
            ctx["side_for_entry"] = side_for_entry
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
            regime = ctx.get("regime") or classify_regime(ctx)

            # 逆張り禁止（ハードルール）
            if not bool(getattr(S, "allow_countertrend", False)):
                if regime == "trend_down" and side_for_entry == "LONG":
                    _bump_skip(state, "regime_not_ok")
                    notify_slack("ℹ️ スキップ: trend_down中のLONG禁止（逆張り抑制）")
                    last_handled_kline = last_start
                    state['last_kline_start'] = last_start
                    save_state(state)
                    time.sleep(float(S.poll_interval_sec))
                    continue
                if regime == "trend_up" and side_for_entry == "SHORT":
                    _bump_skip(state, "regime_not_ok")
                    notify_slack("ℹ️ スキップ: trend_up中のSHORT禁止（逆張り抑制）")
                    last_handled_kline = last_start
                    state['last_kline_start'] = last_start
                    save_state(state)
                    time.sleep(float(S.poll_interval_sec))
                    continue
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

            # 旧 trend_strong_* の代替（MTF整合/強さスコア/フロー）
            need_votes = int(getattr(S, "trend_votes_min", 2))
            need_ofi_z = float(getattr(S, "trend_ofi_z_min", 1.5))
            score_min  = int(getattr(S, "strong_score_min", 4))
            mtf_align  = ctx.get("mtf_align", "none")
            score_up   = int(ctx.get("strong_score_up", 0))
            score_down = int(ctx.get("strong_score_down", 0))
            ofi_local  = float(ctx.get("ofi_z", ofi_z if "ofi_z" in locals() else 0.0))
            strong_up   = (regime == "trend_up")   and ( (edge_votes >= need_votes and ofi_local >=  need_ofi_z) or (mtf_align == "up")   or (score_up   >= score_min) )
            strong_down = (regime == "trend_down") and ( (edge_votes >= need_votes and ofi_local <= -need_ofi_z) or (mtf_align == "down") or (score_down >= score_min) )
            # --- PB flip-follow（cooldown_override がトレンド逆向きに出たら、順張り側へ指値を置く）---
            pb_flip_follow = False
            try:
                if bool(getattr(S, "pb_flip_follow_enable", True)) and override_ok:
                    trend_dir = "LONG" if regime == "trend_up" else ("SHORT" if regime == "trend_down" else None)
                    override_dir = "LONG" if float(ofi_local) >= 0.0 else "SHORT"
                    if trend_dir and (override_dir != trend_dir):
                        # 逆方向のoverride → 順方向に切替し、改めてガードを評価
                        if side_for_entry != trend_dir:
                            try:
                                if trend_dir == "LONG":
                                    ok, why = decide_entry_guard_long(tlist, book, ctx, S)
                                else:
                                    ok, why = decide_entry_guard_short(tlist, book, ctx, S)
                            except Exception as e:
                                ok, why = False, f"guard-eval exception(pb_flip_follow): {e!s}"
                        side_for_entry = trend_dir
                        pb_flip_follow = True
                        relax_note = (relax_note + " | " if relax_note else " | ") + f"pb_flip_follow({regime}: CD={override_dir}→{trend_dir})"
                        try:
                            notify_slack(f"🔁 pb_flip_follow: {regime} + CD-override {override_dir} → {trend_dir}（指値準備）")
                        except Exception:
                            pass
            except Exception:
                pb_flip_follow = False

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

            elif strong_up:
                # 強い上昇トレンド: LONGのみ許可
                if side_for_entry == "SHORT":
                    _bump_skip(state, "regime_not_ok")
                    notify_slack("ℹ️ スキップ: 強い上昇トレンド中のSHORT禁止")
                    last_handled_kline = last_start
                    state['last_kline_start'] = last_start
                    save_state(state)
                    time.sleep(float(S.poll_interval_sec))
                    continue


            elif strong_down:
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

            # micro-entry を使わない（一本化）
                
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

                prof = _decide_tp_sl_profile(regime, side_for_entry, edge_votes, ofi_z, ctx, S)
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
                    # 引き幅k（ATR×k）
                    # 1) pb_flip_follow 時は専用k
                    # 2) ガードが“待ち”で来た場合はレジームに応じて深め（trend_up は min=entry_pullback_atr_trend_min）
                    # 3) それ以外は通常の entry_pullback_atr
                    if 'pb_flip_follow' in locals() and pb_flip_follow:
                        _k = float(getattr(S, "pb_flip_pull_atr", getattr(S, "entry_pullback_atr", 0.25)))
                    elif bool(ctx.get("force_pullback_limit", False)):
                        _base = float(getattr(S, "entry_pullback_atr", 0.25))
                        _trend_min = float(getattr(S, "entry_pullback_atr_trend_min", _base))
                        _k = max(_base, _trend_min) if ctx.get("regime") == "trend_up" else _base
                    else:
                        _k = float(getattr(S, "entry_pullback_atr", 0.25))
                                            # 使う引き幅（ATR×k）
                    pull = float(_k) * float(a)
                    # 5分シグナル直後に板へ PostOnly 指値を即配置
                    if side == "LONG":
                        try:
                            best_bid = float(book["bids"][0][0])
                        except Exception:
                            best_bid = c
                        if 'pb_flip_follow' in locals() and pb_flip_follow:
                            # 押し目拾い：現値より下で待つ
                            limit_px = min(best_bid, c - pull)
                        else:
                            limit_px = min(best_bid, s10 + pull)
                        open_side = "Buy"
                    else:
                        try:
                            best_ask = float(book["asks"][0][0])
                        except Exception:
                            best_ask = c
                        if 'pb_flip_follow' in locals() and pb_flip_follow:
                            # 戻り売り：現値より上で待つ
                            limit_px = max(best_ask, c + pull)
                        else:
                            limit_px = max(best_ask, s10 - pull)
                        open_side = "Sell"

                    res = _place_postonly_fn(S.symbol, open_side, qty, limit_px)
                    if isinstance(res, dict) and res.get("retCode") == 0:
                        placed_postonly = True
                        try:
                            oid = (res.get("result") or {}).get("orderId") or (res.get("result") or {}).get("order_id") or ""
                        except Exception:
                            oid = ""
                        notify_slack(f"🧱 指値配置(PostOnly): {open_side} {limit_px:.4f} | Qty {qty:.4f}" + (f" | id={oid}" if oid else ""))

                        # === 約定監視 ===
                        fill_timeout = int(getattr(S, "postonly_fill_timeout_sec", 120))
                        poll_iv      = float(getattr(S, "postonly_poll_interval_sec", 0.5))
                        allow_part   = bool(getattr(S, "postonly_allow_partial", True))
                        min_ratio    = float(getattr(S, "postonly_min_fill_ratio", 0.5))
                        cancel_to    = bool(getattr(S, "postonly_cancel_on_timeout", True))
                        cancel_rem   = bool(getattr(S, "postonly_cancel_remainder_on_partial", True))

                        t0 = time.time()
                        filled_qty = 0.0
                        avg_fill_px = 0.0
                        last_note_ts = 0.0
                        last_note_sig = ""

                        while True:
                            time.sleep(poll_iv)

                            # 1) 注文状態（Filled/PartiallyFilled など）
                            ord_data = _get_order_rt_fn(S.symbol, oid) if (_get_order_rt_fn and oid) else None
                            items = []
                            if isinstance(ord_data, dict):
                                try:
                                    items = (ord_data.get("result") or {}).get("list") or []
                                except Exception:
                                    items = []
                            od = items[0] if items else {}
                            status = str(od.get("orderStatus", "")) if od else ""

                            try:
                                filled_qty = float(od.get("cumExecQty", od.get("cumQty", 0.0)) or 0.0)
                            except Exception:
                                filled_qty = 0.0
                            try:
                                avg_fill_px = float(od.get("avgPrice", 0.0) or 0.0)
                            except Exception:
                                avg_fill_px = 0.0

                            # 2) 平均約定が空なら、実約定で再集計
                            if filled_qty > 0 and avg_fill_px <= 0 and _get_execs_fn and oid:
                                ex = _get_execs_fn(S.symbol, oid)
                                lst = []
                                try:
                                    lst = (ex.get("result") or {}).get("list") or []
                                except Exception:
                                    lst = []
                                if lst:
                                    _sum_px_qty = 0.0
                                    _sum_qty = 0.0
                                    for e in lst:
                                        try:
                                            q = float(e.get("execQty", 0.0))
                                            p = float(e.get("execPrice", 0.0))
                                        except Exception:
                                            q = 0.0; p = 0.0
                                        _sum_px_qty += p * q
                                        _sum_qty    += q
                                    if _sum_qty > 0:
                                        avg_fill_px = _sum_px_qty / _sum_qty
                                        filled_qty  = _sum_qty

                            full  = filled_qty >= float(qty) * 0.999
                            ratio = (filled_qty / float(qty)) if float(qty) > 0 else 0.0
                            now   = time.time()

                            # 途中経過ログ（状態が変わった時 or 一定間隔）
                            note_iv = float(getattr(S, "postonly_note_interval_sec", 30.0))  # 既定30秒
                            sig = f"{status}|{filled_qty:.4f}/{qty:.4f}"
                            if (now - last_note_ts >= note_iv) or (sig != last_note_sig):
                                last_note_ts = now
                                last_note_sig = sig
                                _log_once(
                                    f"po_note_{oid}",
                                    f"⏳ PostOnly監視: status={status or 'N/A'} "
                                    f"fill={filled_qty:.4f}/{qty:.4f} avg={avg_fill_px or 0.0:.4f}",
                                    5.0  # 同じoidで5秒以内の重複は捨てる保険
                                )

                            # 充足 → state 反映
                            if filled_qty > 0 and (full or (allow_part and ratio >= min_ratio)):
                                sz = float(filled_qty)
                                if (not full) and cancel_rem and _cancel_order_fn and oid:
                                    try:
                                        _cancel_order_fn(S.symbol, oid)
                                    except Exception:
                                        pass

                                # ここから通常エントリー相当の登録
                                c_exec = float(avg_fill_px) if avg_fill_px > 0 else c
                                notional = sz * c_exec
                                fee_rate = float(getattr(S, "maker_fee_rate", getattr(S, "taker_fee_rate", 0.0007)))
                                buy_fee  = notional * fee_rate

                                pos = {
                                    "side": "long" if side == "LONG" else "short",
                                    "entry_price": c_exec,
                                    "qty": sz,
                                    "buy_fee": buy_fee,
                                    "tp_price": tp_price,
                                    "sl_price": sl_price,
                                    "time": datetime.utcnow().isoformat(),
                                    "be_k":  float(prof.get("be_k", 0.0)),
                                    "trail_k": float(prof.get("trail_k", 0.0)),
                                    "profile": str(prof.get("name","")),
                                    "flip": bool(_overrides.get("force_flip", False)),
                                    "risk_sl_dist": abs(c_exec - sl_price),  # ← ここを c ではなく c_exec で
                                }
                                try:
                                    _on_new_entry(state, is_flip=bool(_overrides.get("force_flip")) if '_overrides' in locals() else False)
                                except Exception:
                                    pass
                                state["positions"].append(pos)
                                state["last_entry_time"] = datetime.utcnow().isoformat()

                                prof_name = str(prof.get("name",""))
                                if full:
                                    notify_slack(f"💰 エントリー({side})[PostOnly約定全量]: {c_exec:.4f} | TP {tp_price:.4f} | SL {sl_price:.4f} | Qty {sz:.4f} | 管理={prof_name}{relax_note}")
                                else:
                                    notify_slack(f"💰 エントリー({side})[PostOnly部分約定 {ratio*100:.0f}%]: {c_exec:.4f} | TP {tp_price:.4f} | SL {sl_price:.4f} | Qty {sz:.4f} | 管理={prof_name}{relax_note}")

                                last_handled_kline = last_start
                                state["last_kline_start"] = last_start
                                save_state(state)
                                time.sleep(float(S.poll_interval_sec))
                                break

                            # タイムアウト
                            if (now - t0) > float(fill_timeout):
                                # まずはキャンセル要求
                                if cancel_to and _cancel_order_fn and oid:
                                    try:
                                        _cancel_order_fn(S.symbol, oid)
                                        notify_slack(f"🧹 PostOnlyキャンセル（timeout {fill_timeout}s） id={oid}")
                                    except Exception as e:
                                        notify_slack(f":x: PostOnlyキャンセル失敗: {e}")

                                # キャンセル直後の実状態を必ず確認（部分約定はここで取り込む）
                                st_now, fq_now, ap_now = _order_status_local(oid)
                                if fq_now and fq_now > 0.0:
                                    _adopt_position_from_fill(
                                        side, float(fq_now), float(ap_now or 0.0) or float(c),
                                        float(tp_price), float(sl_price), prof, _overrides if '_overrides' in locals() else {}
                                    )
                                    last_handled_kline = last_start
                                    state['last_kline_start'] = last_start
                                    save_state(state)
                                    time.sleep(float(S.poll_interval_sec))
                                    break

                                # まだ未キャンセル/未約定 → ウォッチリストへ登録して継続監視
                                state.setdefault("watch_orders", []).append({
                                    "oid": oid, "side": side, "qty": float(qty),
                                    "tp": float(tp_price), "sl": float(sl_price),
                                    "prof": prof, "overrides": _overrides if '_overrides' in locals() else {},
                                    "_created": time.time(), "last_price": float(c),
                                })
                                _bump_skip(state, "other")
                                notify_slack(
                                    f"ℹ️ スキップ: PostOnly未充足 timeout（fill={filled_qty:.4f}/{qty:.4f}）→監視に移行"
                                )
                                last_handled_kline = last_start
                                state['last_kline_start'] = last_start
                                save_state(state)
                                time.sleep(float(S.poll_interval_sec))
                                break
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
                                "risk_sl_dist": abs(c - sl_price),   # ← 追加（成行/簡易APIは c が建値）
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
