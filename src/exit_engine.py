# exit_engine.py
from __future__ import annotations
from typing import Dict, Any
from datetime import datetime, timezone

# 既存ヘルパ利用
from .flow_filters import compute_flow_metrics, compute_wall_pressure
from .flow_filters_dynamic import classify_regime


def _edge_metrics_snapshot(edge) -> Dict[str, float | int]:
    met: Dict[str, Any] = {}
    try:
        if edge and hasattr(edge, "get_metrics_snapshot"):
            met = edge.get_metrics_snapshot() or {}
    except Exception:
        met = {}
    def _f(x, t=float):
        try:
            return t(x)
        except Exception:
            return 0
    return {
        "ofi_z": _f(met.get("ofi_z", 0.0), float),
        "cons_buy": _f(met.get("cons_buy", 0), int),
        "cons_sell": _f(met.get("cons_sell", 0), int),
        "cvd_slope_z": _f(met.get("cvd_slope_z", 0.0), float),
    }


def _pos_key(pos: Dict[str, Any]) -> str:
    # 位置一意キー（既存のpos["time"]がISO想定）
    return str(pos.get("time") or pos.get("entry_time") or "")


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def evaluate(
    pos: Dict[str, Any],
    ctx: Dict[str, Any],
    book: Dict[str, Any],
    trades: list[Dict[str, Any]],
    edge: Any,
    state: Dict[str, Any],
    S: Any,
    h: float | None = None,
    l: float | None = None
) -> Dict[str, Any]:
    """
    Exit Engine の最小実用版（雛形）
    戻り値:
      {"action": "HOLD|TP_PART|TP_ALL|CUT|UPDATE_SL|SL_GRACE",
       "ratio": 0.5, "new_sl": float, "reason": "str", "grace_sec": int}
    """
    if not bool(getattr(S, "exit_engine_enable", True)):
        return {"action": "HOLD"}

    side = (pos.get("side") or "long").lower()
    c = float(ctx.get("price", 0.0))
    a = float(ctx.get("atr", 0.0)) or 1e-9
    s10 = float(ctx.get("sma10", c))
    regime = classify_regime(ctx)
    em = _edge_metrics_snapshot(edge)

    tp = float(pos.get("tp_price", c * (1.0 + 0.02)))  # フェイルセーフ
    sl = float(pos.get("sl_price", c * (1.0 - 0.02)))
    ep = float(pos.get("entry_price", c))

    # ---- パラメータ（config未定義でも動くよう既定値を持つ） ----
    tp_near_bps = float(getattr(S, "tp_near_bps", 10.0))              # 10bps=0.10%
    tp_near_atr_k = float(getattr(S, "tp_near_atr_k", 0.10))          # 0.10*ATR
    wick_body_ratio_min = float(getattr(S, "wick_body_ratio_min", 2.0))
    early_votes_need = int(getattr(S, "early_tp_votes_needed", 2))
    tp_part_ratio = float(getattr(S, "tp_part_ratio", 0.5))

    sl_grace_enable = bool(getattr(S, "sl_grace_enable", True))
    sl_grace_bps = float(getattr(S, "sl_grace_bps", 6.0))
    sl_grace_sec = int(getattr(S, "sl_grace_sec", 20))
    sl_grace_need = int(getattr(S, "sl_grace_need_votes", 2))

    time_stop_min = int(getattr(S, "time_stop_min", 7))
    min_follow_R = float(getattr(S, "min_follow_through_R", 0.4))

    # OBの閾値（エントリ用より少し緩め）: ask強すぎ=レジスタンス濃厚
    ob_depth = int(getattr(S, "ob_depth", 50))
    ob_ratio_exit = max(1.2, float(getattr(S, "ob_ask_bid_max_trend", 1.40)) - 0.2)

    # ---- 事前計算 ----
    price_bps = c * (tp_near_bps / 10000.0)
    near_thr = max(price_bps, tp_near_atr_k * a)
    if side == "long":
        dist_tp = max(0.0, tp - c)
        dist_sl = max(0.0, c - sl)
    else:
        dist_tp = max(0.0, c - tp)
        dist_sl = max(0.0, sl - c)

    # 上ヒゲ/下ヒゲ簡易推定（open不明のため close/low or high/close を近似利用）
    if h is None:
        h = float(ctx.get("hh", c))
    if l is None:
        l = float(ctx.get("ll", c))
    if side == "long":
        upper_wick = max(0.0, h - c)
        body = max(1e-9, c - l)
        wick_ratio = upper_wick / body
    else:
        lower_wick = max(0.0, c - l)
        body = max(1e-9, h - c)
        wick_ratio = lower_wick / body

    macd_peakout = (float(ctx.get("macd_hist", 0.0)) <
                    float(ctx.get("macd_hist_prev", 0.0)))

    # OB/Flow
    ob_ratio, ob_ask, ob_bid = compute_wall_pressure(book, ob_depth)
    flow_s = compute_flow_metrics(trades or [], window_sec=30)
    rate_usd = float(flow_s.get("rate_usd", 0.0))

    # 直近フロー比較（stateに保存）
    st = state.setdefault("exit_engine", {})
    fk = f"flow_prev_{side}"
    prev_rate = float(st.get(fk, 0.0))
    st[fk] = rate_usd
    flow_worse = (rate_usd < prev_rate) or (side == "long" and rate_usd <= 0) or (side == "short" and rate_usd >= 0)

    # ===== A) 早期利確（Near-TP + 票決） =====
    near_tp = (dist_tp <= near_thr)
    votes = 0
    if near_tp and wick_ratio >= wick_body_ratio_min:
        votes += 1
    if near_tp and macd_peakout:
        votes += 1
    if near_tp and ((side == "long" and ob_ratio >= ob_ratio_exit) or (side == "short" and ob_ratio <= (1.0 / ob_ratio_exit))):
        votes += 1
    if near_tp and flow_worse:
        votes += 1

    if near_tp and votes >= early_votes_need:
        # trendは部分→様子見、range/neutralは即全決済でもよい
        if regime in ("range", "neutral"):
            return {"action": "TP_ALL", "reason": f"early_take_profit[{votes}votes] nearTP"}
        return {
            "action": "TP_PART",
            "ratio": max(0.1, min(1.0, tp_part_ratio)),
            "reason": f"early_take_profit[{votes}votes] nearTP",
        }

    # ===== B) SLグレース（ヒゲ救済） =====
    if sl_grace_enable:
        price_bps_sl = c * (sl_grace_bps / 10000.0)
        near_sl = (dist_sl <= max(price_bps_sl, 0.06 * a))
        # 支持条件：MA維持 + OBがbid優勢 + OFI/flowが弱くない
        ob_bid_favored = (ob_ratio < 1.0)  # bid > ask
        ofi_ok = float(em.get("ofi_z", 0.0)) >= 0.0 if side == "long" else float(em.get("ofi_z", 0.0)) <= 0.0
        ma_ok = (c >= (s10 - 0.15 * a)) if side == "long" else (c <= (s10 + 0.15 * a))
        votes_sl = int(ob_bid_favored) + int(ofi_ok) + int(ma_ok)
        if near_sl and votes_sl >= sl_grace_need:
            return {
                "action": "SL_GRACE",
                "grace_sec": max(5, sl_grace_sec),
                "reason": f"sl_grace[{votes_sl}votes] keep_support",
            }

    # ===== C) 失速撤退（タイムストップ） =====
    try:
        et = pos.get("time") or pos.get("entry_time")
        et_dt = datetime.fromisoformat(str(et))
        held_min = (datetime.now(timezone.utc) - et_dt).total_seconds() / 60.0
    except Exception:
        held_min = 0.0

    # R計算（初期リスク距離で正規化）
    r0 = abs(float(pos.get("risk_sl_dist", (ep - sl) if side == "long" else (sl - ep)))) or 1e-9
    cur_R = ((c - ep) / r0) if side == "long" else ((ep - c) / r0)
    pk_key = f"peak_R_{_pos_key(pos)}"
    prev_pk = float(st.get(pk_key, 0.0))
    st[pk_key] = max(prev_pk, cur_R)

    if held_min >= float(time_stop_min) and st[pk_key] < float(min_follow_R):
        return {"action": "CUT", "reason": f"time_stop({held_min:.1f}m, peakR={st[pk_key]:.2f}<{min_follow_R})"}

    # 何もしない
    return {"action": "HOLD"}
