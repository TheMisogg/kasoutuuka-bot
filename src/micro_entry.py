# micro_entry.py
from __future__ import annotations
import time, math
from typing import Callable, Dict, List, Optional, Tuple

Number = float

def _apply_bps(price: Number, bps: Number, side: str, sign: int = 1) -> Number:
    """bps を price に適用。side は 'LONG'/'SHORT'。sign=1で正方向, -1で逆方向。"""
    ratio = (bps or 0.0) / 10000.0
    if side == "LONG":
        return float(price) * (1.0 - ratio * sign)
    else:
        return float(price) * (1.0 + ratio * sign)

def _sr_levels_from_5m(last5m: Dict[str, List[Number]], lookback: int, buffer_bps: Number) -> Tuple[Optional[Number], Optional[Number]]:
    highs = list(last5m.get("high", []))
    lows  = list(last5m.get("low", []))
    if not highs or not lows:
        return None, None
    hh = max(highs[-max(1, lookback):])
    ll = min(lows[-max(1, lookback):])
    # 少し内側に寄せる（触れやすく、割れ判定は外側へ）
    lo = _apply_bps(ll, buffer_bps, "LONG", sign=-1)   # LONG用の押し目はやや下へ
    hi = _apply_bps(hh, buffer_bps, "SHORT", sign=-1)  # SHORT用の戻りはやや上へ
    return lo, hi

def compute_pullback_target(
    *,
    side: str,
    now_price: Number,
    last5m: Dict[str, List[Number]],
    use_1m: bool,
    get_1m_ema_atr: Optional[Callable[[], Tuple[Optional[Number], Optional[Number]]]],
    sr_lookback: int,
    sr_buffer_bps: Number,
    pullback_k_atr: Number,
    improve_bps: Number,
) -> Tuple[Number, Optional[Number], Optional[Number], str]:
    """
    ターゲット価格と S/R を返す。1分EMA±k*ATR と 5分S/R を統合し、現値より“必ず有利”を強制。
    """
    sr_low, sr_high = _sr_levels_from_5m(last5m, sr_lookback, sr_buffer_bps)
    ema1m = atr1m = None
    if use_1m and get_1m_ema_atr is not None:
        try:
            ema1m, atr1m = get_1m_ema_atr()
        except Exception:
            ema1m = atr1m = None

    cands: List[Number] = []
    note_parts: List[str] = []
    if side == "LONG":
        if sr_low is not None:
            cands.append(sr_low); note_parts.append(f"SR_low={sr_low:.4f}")
        if ema1m is not None and atr1m is not None:
            cands.append(ema1m - pullback_k_atr * atr1m); note_parts.append(f"EMA1m-{pullback_k_atr}*ATR1m")
        cands.append(_apply_bps(now_price, improve_bps, "LONG", sign=1)); note_parts.append(f"improve {improve_bps}bps")
        target = max(cands)
        if target >= now_price:
            target = _apply_bps(now_price, improve_bps, "LONG", sign=1)
    else:
        if sr_high is not None:
            cands.append(sr_high); note_parts.append(f"SR_high={sr_high:.4f}")
        if ema1m is not None and atr1m is not None:
            cands.append(ema1m + pullback_k_atr * atr1m); note_parts.append(f"EMA1m+{pullback_k_atr}*ATR1m")
        cands.append(_apply_bps(now_price, improve_bps, "SHORT", sign=1)); note_parts.append(f"improve {improve_bps}bps")
        target = min(cands)
        if target <= now_price:
            target = _apply_bps(now_price, improve_bps, "SHORT", sign=1)

    return float(target), sr_low, sr_high, " | ".join(note_parts)

def wait_for_micro_entry(

    side: str,
    target: Number,
    get_now_price: Callable[[], Number],
    sr_low: Optional[Number],
    sr_high: Optional[Number],
    invalidation_extra_bps: Number,
    max_wait_sec: int,
) -> Tuple[bool, Number, str]:
    """
    価格が target に達するのを最長 max_wait_sec だけ待つ。
    S/R を明確に割れたら無効化（未約定のまま終了）。
    戻り値: (executed, exec_price, note)
    """
    t0 = time.time()
    inv_note = ""
    while True:
        px = float(get_now_price())
        if side == "LONG":
            if px <= target:
                return True, px, f"reached {px:.4f}<=target {target:.4f}"
            if sr_low is not None:
                inv = _apply_bps(sr_low, invalidation_extra_bps, "SHORT", sign=1)
                if px < inv:
                    inv_note = f"S/R invalidated: {px:.4f} < {inv:.4f}"
                    break
        else:  # SHORT
            if px >= target:
                return True, px, f"reached {px:.4f}>=target {target:.4f}"
            if sr_high is not None:
                inv = _apply_bps(sr_high, invalidation_extra_bps, "LONG", sign=1)
                if px > inv:
                    inv_note = f"S/R invalidated: {px:.4f} > {inv:.4f}"
                    break
        if (time.time() - t0) > max_wait_sec:
            return False, px, f"timeout {max_wait_sec}s (last {px:.4f})"
        time.sleep(0.5)
    return False, float(get_now_price()), inv_note or "invalidated"


