"""
flow_filters_dynamic.py — dynamic entry guard with explain tags.
- Wait-for-pullback (≤ SMA10 + α·ATR) where α can widen in trend.
- Hard cap on distance from SMA10.
- Orderbook soft guard with strong-flow override.
- Two-window flow check (short/long).
"""
from __future__ import annotations
from typing import Dict, Any, Tuple

from .config import STRATEGY as S
from .flow_filters import compute_flow_metrics, compute_wall_pressure

# =========================
# 強制フリップ（反転）サポート
# =========================
from typing import Any, Dict, Tuple

def _get_edge_from_state_or_global(st: dict | None = None, default=None):
    # state優先 → グローバル変数 → 明示渡し
    try:
        if isinstance(st, dict) and st.get("edge") is not None:
            return st.get("edge")
    except Exception:
        pass
    try:
        # main 側で global edge を使っている場合に拾う
        return globals().get("edge", default)
    except Exception:
        return default

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

def _estimate_net_qty_from_state(st: dict | None) -> float:
    """ローカルstateからネット玉（数量の絶対値）をざっくり推定"""
    if not isinstance(st, dict):
        return 0.0
    qty = 0.0
    try:
        for p in st.get("positions", []):
            side = (p.get("side") or "").lower()
            size = float(p.get("size") or p.get("qty") or 0.0)
            if side == "long":
                qty += size
            elif side == "short":
                qty -= size
        return abs(qty)
    except Exception:
        return 0.0

def _should_force_flip(requested_side: str, edge, *, S=None) -> Tuple[bool, Dict[str, Any]]:
    """
    強い逆方向フロー時にフリップを許可するかを判定。
    2票以上でTrue（既定）：OFI|連続買い/売り|CVD傾きZ の多数決
    """
    if S is None:
        # Sはグローバル設定オブジェクト想定
        try:
            S = globals().get("S")
        except Exception:
            S = None
    if S is not None and not bool(getattr(S, "flip_enable", True)):
        return False, {}

    m = _edge_metrics_snapshot(edge)
    def _clip(z, clip=None):
        clip = float(getattr(S, "ofi_z_clip", 6.0)) if clip is None else float(clip)
        try:
            zf=float(z)
        except Exception:
            return 0.0, False
        sus = abs(zf) > (clip*1.5)
        if zf > clip:  zf = clip
        if zf < -clip: zf = -clip
        return zf, sus

    ofi_raw = m.get("ofi_z", 0.0)
    ofi, ofi_sus = _clip(ofi_raw)
    cons_buy = int(m.get("cons_buy", 0))
    cons_sell = int(m.get("cons_sell", 0))
    cvd_z = float(m.get("cvd_slope_z", 0.0))

    th_ofi = float(getattr(S, "flip_ofi_z", 2.0)) if S is not None else 2.0
    th_cons = int(getattr(S, "flip_cons", 3)) if S is not None else 3
    th_cvd  = float(getattr(S, "flip_cvd_z", 1.5)) if S is not None else 1.5

    votes = 0
    need = int(getattr(S, "flip_votes_needed", 2))
    # OFIがsuspectの時は最低2票を維持（= 単独でフリップ許可しない）
    if ofi_sus and need < 2:
        need = 2
    return (votes >= need, {"metrics": m, "ofi_clipped": ofi, "ofi_suspect": ofi_sus})

def classify_regime(ctx: Dict[str, Any]) -> str:
    price = float(ctx.get("price", 0.0)) or 1.0
    atr = float(ctx.get("atr", 0.0))
    atrp = (atr / price) if price else 0.0
    adx = float(ctx.get("adx", 0.0))
    s10 = float(ctx.get("sma10", price))
    s50 = float(ctx.get("sma50", s10))
    
    # トレンド判定を厳格化
    trend_strength = 0
    if (atrp >= float(S.atrp_trend_min) or adx >= float(S.adx_trend_min)):
        trend_strength += 1
    if s10 >= s50:
        trend_strength += 1
    if ctx.get("macd", 0) > ctx.get("macd_sig", 0):
        trend_strength += 1
    
    # 強いトレンド判定
    if trend_strength >= 2 and adx >= 25:  # ADXが25以上で強いトレンド
        if s10 > s50:
            return "trend_strong_long"
        else:
            return "trend_strong_short"
    elif trend_strength >= 2:
        return "trend_up"
    
    # レンジ判定を改善
    if atrp < 0.004 and abs(s10 - s50) < (0.3 * atr):  # より厳格なレンジ判定
        return "range"
    
    return "neutral"

# レンジ上限/下限判定関数を追加
def is_range_upper(ctx: Dict[str, Any]) -> bool:
    """レンジ上限付近か判定"""
    try:
        hh = float(ctx.get("hh", 0))
        ll = float(ctx.get("ll", 0))
        price = float(ctx.get("price", 0))
        if hh <= ll:
            return False
        range_position = (price - ll) / (hh - ll)
        return range_position >= 0.7  # レンジ上位30%で上限と判定
    except:
        return False

def is_range_lower(ctx: Dict[str, Any]) -> bool:
    """レンジ下限付近か判定"""
    try:
        hh = float(ctx.get("hh", 0))
        ll = float(ctx.get("ll", 0))
        price = float(ctx.get("price", 0))
        if hh <= ll:
            return False
        range_position = (price - ll) / (hh - ll)
        return range_position <= 0.3  # レンジ下位30%で下限と判定
    except:
        return False

# ===== BEAR REGIME GUARD (trend-first) =====
# 逆張りLONG多発を止め、ベア環境ではSHORTに寄せる。カピチュレーションLONGのみ例外で許可。

def _is_bear_regime(ctx: Dict[str, Any]) -> bool:
    """簡易ベア判定: SMA10<SMA50 かつ MACD<Signal"""
    s10 = float(ctx.get("sma10", 0.0))
    s50 = float(ctx.get("sma50", s10))
    m   = float(ctx.get("macd", 0.0))
    ms  = float(ctx.get("macd_sig", 0.0))
    return (s10 < s50) and (m < ms)

def _is_capitulation_long(ctx: Dict[str, Any]) -> bool:
    """ショート清算・OI急減・強い買いOFIで“逆張りLONGスキャル”のみ許可"""
    ofi_z = float(ctx.get("ofi_z", 0.0))
    liq_s = float(ctx.get("liq_short_usd", 0.0))
    oi_dp = float(ctx.get("oi_drop_pct", 0.0))   # 例: -0.8 → -0.8%
    return (ofi_z >= 2.0) and (liq_s >= 3_000_000.0) and (oi_dp <= -0.7)

def _is_capitulation_short(ctx: Dict[str, Any]) -> bool:
    """ロング清算・OI急減・強い売りOFIで“逆張りSHORTスキャル”のみ許可"""
    ofi_z = float(ctx.get("ofi_z", 0.0))
    liq_l = float(ctx.get("liq_long_usd", 0.0))
    oi_dp = float(ctx.get("oi_drop_pct", 0.0))
    return (ofi_z <= -2.0) and (liq_l >= 3_000_000.0) and (oi_dp <= -0.7)


def decide_entry_guard_long(trades: list, book: dict, ctx: Dict[str, Any], S=S) -> Tuple[bool, str]:
    price = float(ctx.get("price", 0.0))
    s10   = float(ctx.get("sma10", price))
    s50   = float(ctx.get("sma50", s10))
    atr   = float(ctx.get("atr", 0.0)) or 1e-9
    rsi   = float(ctx.get("rsi", ctx.get("rsi14", 50.0)))

    # --- SMA10 / RSI の絶対ガード ---
    if getattr(S, "require_close_gt_sma10_long", True) and not (price > s10):
        return (False, "close≤SMA10(guard)")
    if rsi < float(getattr(S, "rsi_long_min", 55.0)):
        return (False, f"RSI<{int(getattr(S, 'rsi_long_min', 55))}(guard)")    

    regime = classify_regime(ctx)
    relax_tags = []
    # === ブル・レジーム中は SHORT 原則禁止（例外：カピチュレーションSHORT） ===
    if regime == "trend_up":
        if _is_capitulation_short(ctx):
            ctx["mode"] = "capitulation_short"  # main.py 側でサイズ/TP調整
            return (True, "capitulation_short")
        return (False, "Bull regime: short disabled")

    # --- (1) Pullback rule ---
    alpha_base = float(getattr(S, "entry_pullback_atr", 0.25))
    alpha = alpha_base
    if regime == "trend_up":
        alpha = max(alpha, float(getattr(S, "entry_pullback_atr_trend_min", 0.35)))
        if alpha > alpha_base:
            relax_tags.append(f"trend_widen→{alpha:.2f}ATR")

    target = s10 + alpha * atr
    dist_atr = (price - s10) / atr

    # ---- 距離ハードキャップ（基準値）を決めてから動的ボーナスを算出 ----
    k_trend   = float(getattr(S, "entry_max_over_sma10_atr_trend", 1.50))
    k_neutral = float(getattr(S, "entry_max_over_sma10_atr_neutral", 0.70))
    k_range   = float(getattr(S, "entry_max_over_sma10_atr_range", 0.55))
    k_cap_base = k_trend if regime == "trend_up" else (k_range if regime == "range" else k_neutral)

    votes = int(ctx.get("edge_votes", 0))
    ofi_z = float(ctx.get("ofi_z", 0.0))
    bonus = 0.0
    if bool(getattr(S, "cap_bonus_enabled", True)) and regime != "trend_up":
        # votes ボーナス
        if votes >= 2:
            bonus += float(getattr(S, "cap_bonus_votes2", 0.05))
        if votes >= 3:
            bonus += (votes - 2) * float(getattr(S, "cap_bonus_per_vote", 0.08))
        # OFI z ボーナス
        z0 = float(getattr(S, "ofi_z_boost_thr", 2.0))
        if ofi_z >= z0:
            bonus += (ofi_z - z0) * float(getattr(S, "cap_bonus_ofi_z_k", 0.06))
        # 上限
        bonus_max = float(getattr(S, "cap_bonus_range_max" if regime=="range" else "cap_bonus_neutral_max", 0.30))
        if bonus > bonus_max: bonus = bonus_max
    k_cap_dyn = k_cap_base + max(0.0, bonus)

    # strong short-window flow can override pullback
    # === ベア・レジーム中は LONG 原則禁止（例外：カピチュレーションLONG） ===
    if _is_bear_regime(ctx):
        if _is_capitulation_long(ctx):
            ctx["mode"] = "capitulation_long"   # main.py 側でサイズ/TP調整
            return (True, "capitulation_long")
        return (False, "Bear regime: long disabled")

    # strong short-window flow can override pullback
    fm_s = compute_flow_metrics(trades, int(getattr(S, "flow_window_short_sec", 30)))

    override_rate = float(getattr(S, "pullback_override_rateS", 9000.0))
    override_net  = float(getattr(S, "pullback_override_netS", 80000.0))
    if price > target:
        allow_by_flow = (fm_s.get("rate_usd", 0.0) >= override_rate) and (fm_s.get("net_usd", 0.0) >= override_net)
        extra = 0.0
        if regime == "neutral":
            extra = float(getattr(S, "momentum_extra_atr_neutral", 0.30))  # ←追加パラメータ
        allow_by_momentum = bool(getattr(S, "use_momentum_pullback_override", True)) \
                            and votes >= int(getattr(S, "momentum_votes_min", 2)) \
                            and (dist_atr <= (k_cap_dyn + extra))
        if allow_by_flow:
            relax_tags.append("strong_flow_override")
        elif allow_by_momentum:
            relax_tags.append("momentum_override")
        else:
           # --- Pivot-OB override: SMA10 近辺で bid 優勢なら押し目成立とみなす ---
           if bool(getattr(S, "use_pivot_ob_override", True)):
               ob_ratio, _, _ = compute_wall_pressure(book, int(getattr(S, "ob_depth",50)))
               pivot_max_dist = float(getattr(S, "pivot_max_dist_atr", 1.20))   # 目安: ≤1.2ATR上
               want =  float(getattr(S, "pivot_ob_max_ratio", 0.75))            # ask/bid ≤ 0.75（bid優勢）
               need_z = float(getattr(S, "pivot_min_ofi_z", 1.2))               # 最低限のOFI z
               if (dist_atr <= pivot_max_dist) and (ob_ratio <= want) and (ofi_z >= need_z) and (votes >= 2):
                   relax_tags.append("pivot_ob_override")
               else:
                    why = f"押し目待ち: ≤SMA10+{alpha:.2f}ATR (now +{dist_atr:.2f}ATR)"
                    if relax_tags: why += " | relax=" + ",".join(relax_tags)
                    return (False, why)


    # --- (2) Hard cap distance（動的ボーナス適用後） ---
    if dist_atr > k_cap_dyn:
        if bonus > 0: relax_tags.append(f"cap_bonus=+{bonus:.2f}")
        why = f"距離>SMA10+{k_cap_dyn:.2f}ATR (dist=+{dist_atr:.3f}ATR)"
        if relax_tags: why += " | relax=" + ",".join(relax_tags)
        return (False, why)

    # --- (3) Orderbook soft guard ---
    if bool(getattr(S, "use_orderbook_filter", True)):
        ob_ratio, _, _ = compute_wall_pressure(book, int(getattr(S, "ob_depth", 50)))
        base_max = float(getattr(S, "ob_ask_bid_max_trend" if regime == "trend_up" else ("ob_ask_bid_max_range" if regime=="range" else "ob_ask_bid_max_neutral"), 0.80))
        relax = float(getattr(S, "ob_relax_band", 0.05))
        ob_ok = (ob_ratio <= (base_max + relax))
        if not ob_ok:
            # allow override by strong flow
            rateS_th = float(getattr(S, "ob_override_rateS", 6000.0))
            netS_th  = float(getattr(S, "ob_override_netS", 50000.0))
            if (fm_s.get("rate_usd",0.0) >= rateS_th) and (fm_s.get("net_usd",0.0) >= netS_th):
                relax_tags.append("ob_strong_flow_override")
            else:
                return (False, f"OB警戒: {ob_ratio:.2f} (max {base_max:.2f}+{relax:.2f}) | flow弱 (rateS={int(fm_s.get('rate_usd',0))}/s netS={int(fm_s.get('net_usd',0))})")

    # --- (4) Two-window flow baseline ---
    fm_l = compute_flow_metrics(trades, int(getattr(S, "flow_window_long_sec", 60)))
    flow_ok = (
        (fm_s.get("count",0) >= int(getattr(S, "flow_min_count", 120))) and
        (fm_s.get("consec",0) >= int(getattr(S, "flow_min_consec",8))) and
        (fm_s.get("imbalance",0.0) >= float(getattr(S, "flow_min_imbalance",0.25))) and
        (fm_s.get("net_usd",0.0)   >= float(getattr(S, "flow_min_net_usd",50000.0)))
    )
    if not flow_ok:
        why = (f"flow不足: cnt={int(fm_s.get('count',0))} / consec={int(fm_s.get('consec',0))} "
               f"/ imb={float(fm_s.get('imbalance',0.0)):.2f} / netS={int(fm_s.get('net_usd',0.0))} "
               f"netL={int(fm_l.get('net_usd',0.0))}")
        if relax_tags:
            why += " | relax=" + ",".join(relax_tags)
        return (False, why)

    # OK
    ok_msg = (f"OK | regime={regime} dist=+{dist_atr:.2f}ATR (cap {k_cap_dyn:.2f}) | "
              f"flowS(rate={int(fm_s.get('rate_usd',0))}/s net={int(fm_s.get('net_usd',0))}) "
              f"flowL(rate={int(fm_l.get('rate_usd',0))}/s net={int(fm_l.get('net_usd',0))})")
    if bool(getattr(S, "use_orderbook_filter", True)):
        ob_ratio, _, _ = compute_wall_pressure(book, int(getattr(S, 'ob_depth',50)))
        base_max = float(getattr(S, "ob_ask_bid_max_trend" if regime == "trend_up" else ("ob_ask_bid_max_range" if regime=="range" else "ob_ask_bid_max_neutral"), 0.80))
        relax = float(getattr(S, "ob_relax_band", 0.05))
        ok_msg += f" | ob={ob_ratio:.2f} (max {base_max:.2f}±{relax:.2f})"
    if relax_tags:
        ok_msg += " | relax=" + ",".join(relax_tags)
    return (True, ok_msg)

def decide_entry_guard_short(trades: list, book: dict, ctx: Dict[str, Any], S=S) -> Tuple[bool, str]:
    """
    SHORT版のエントリーガード。
    decide_entry_guard_long と同一インターフェース／同等の判定を「反転」して実装。
    - プルバック判定は「SMA10 - α·ATR」までの戻り待ち
    - 距離ハードキャップは「SMA10 から下方向の距離」を監視
    - Orderbook フィルタは ask 優勢（ask/bid が十分大）を優先
    - フロー判定は sell 優勢（net_usd が十分にマイナス、imbalance が負）を要求
    戻り値: (ok, reason)
    """
    price = float(ctx.get("price", 0.0))
    s10   = float(ctx.get("sma10", price))
    s50   = float(ctx.get("sma50", s10))
    atr   = float(ctx.get("atr", 0.0)) or 1e-9
    rsi   = float(ctx.get("rsi", ctx.get("rsi14", 50.0)))

    # --- SMA10 / RSI の絶対ガード ---
    if getattr(S, "require_close_lt_sma10_short", True) and not (price < s10):
        return (False, "close≥SMA10(guard)")
    if rsi > float(getattr(S, "rsi_short_max", 50.0)):
        return (False, f"RSI>{int(getattr(S, 'rsi_short_max', 50))}(guard)")

    regime = classify_regime(ctx)  # "trend_up" / "neutral" / "range"
    relax_tags = []

    # --- (1) Pullback rule for SHORT (上方向への戻り待ち) ---
    alpha_base = float(getattr(S, "entry_pullback_atr", 0.25))
    alpha = alpha_base
    if regime == "trend_up":
        # 上昇トレンドではショートはより慎重に（=より深い戻りを要求）
        alpha = max(alpha, float(getattr(S, "entry_pullback_atr_trend_min", 0.35)))
        if alpha > alpha_base:
            relax_tags.append(f"trend_widen→{alpha:.2f}ATR")

    target = s10 - alpha * atr
    # 下方向の距離（SMA10からどれだけ下に離れているか）
    dist_atr = (s10 - price) / atr

    # ---- 距離ハードキャップ（基準値）→ 動的ボーナス（左右対称で流用）----
    k_trend   = float(getattr(S, "entry_max_over_sma10_atr_trend", 1.50))
    k_neutral = float(getattr(S, "entry_max_over_sma10_atr_neutral", 0.70))
    k_range   = float(getattr(S, "entry_max_over_sma10_atr_range", 0.55))
    k_cap_base = k_trend if regime == "trend_up" else (k_range if regime == "range" else k_neutral)

    votes = int(ctx.get("edge_votes", 0))
    ofi_z = float(ctx.get("ofi_z", 0.0))
    bonus = 0.0
    if bool(getattr(S, "cap_bonus_enabled", True)) and regime != "trend_up":
        if votes >= 2:
            bonus += float(getattr(S, "cap_bonus_votes2", 0.05))
            if votes >= 3:
                bonus += (votes - 2) * float(getattr(S, "cap_bonus_per_vote", 0.08))
        z0 = float(getattr(S, "ofi_z_boost_thr", 2.0))
        # SHORT側は“売り優勢”を見るので ofi_zの負側を評価
        if ofi_z <= -z0:
            bonus += (abs(ofi_z) - z0) * float(getattr(S, "cap_bonus_ofi_z_k", 0.06))
        bonus_max = float(getattr(S, "cap_bonus_range_max" if regime=="range" else "cap_bonus_neutral_max", 0.30))
        if bonus > bonus_max:
            bonus = bonus_max
    k_cap_dyn = k_cap_base + max(0.0, bonus)

    # --- strong short-window flow can override pullback（下に走る勢いで上書き）---
    fm_s = compute_flow_metrics(trades, int(getattr(S, "flow_window_short_sec", 30)))
    override_rate = float(getattr(S, "pullback_override_rateS", 9000.0))
    override_net  = float(getattr(S, "pullback_override_netS", 80000.0))
    if price < target:
        # rate は絶対値、net は十分にマイナスで“強い売り”とみなす
        allow_by_flow = (abs(fm_s.get("rate_usd", 0.0)) >= override_rate) and (fm_s.get("net_usd", 0.0) <= -override_net)
        extra = 0.0
        if regime == "neutral":
            extra = float(getattr(S, "momentum_extra_atr_neutral", 0.30))
        allow_by_momentum = bool(getattr(S, "use_momentum_pullback_override", True)) \
                            and votes >= int(getattr(S, "momentum_votes_min", 2)) \
                            and (dist_atr <= (k_cap_dyn + extra))
        if allow_by_flow:
            relax_tags.append("strong_flow_override")
        elif allow_by_momentum:
            relax_tags.append("momentum_override")
        else:
            # --- Pivot-OB override: SMA10近辺で ask 優勢（頭上の売り板）なら戻り売り成立 ---
            if bool(getattr(S, "use_pivot_ob_override", True)):
                ob_ratio, _, _ = compute_wall_pressure(book, int(getattr(S, "ob_depth",50)))
                pivot_max_dist = float(getattr(S, "pivot_max_dist_atr", 1.20))   # 目安: ≤1.2ATR下
                want_min = 1.0 / max(1e-9, float(getattr(S, "pivot_ob_max_ratio", 0.75)))  # ask/bid ≥ 1/(0.75)=1.33...
                need_z   = float(getattr(S, "pivot_min_ofi_z", 1.2))
                if (dist_atr <= pivot_max_dist) and (ob_ratio >= want_min) and (ofi_z <= -need_z) and (votes >= 2):
                    relax_tags.append("pivot_ob_override")
                else:
                    why = f"戻り売り待ち: ≥SMA10-{alpha:.2f}ATR (now -{dist_atr:.2f}ATR)"
                    if relax_tags:
                        why += " | relax=" + ",".join(relax_tags)
                    return (False, why)

    # --- (2) 距離ハードキャップ ---
    if dist_atr > k_cap_dyn:
        why = f"距離<SMA10-{k_cap_dyn:.2f}ATR (dist=-{dist_atr:.3f}ATR)"
        if relax_tags:
            why += " | relax=" + ",".join(relax_tags)
        return (False, why)

    # --- (3) Orderbook soft guard（ask 優勢を要求）---
    if bool(getattr(S, "use_orderbook_filter", True)):
        ob_ratio, _, _ = compute_wall_pressure(book, int(getattr(S, "ob_depth", 50)))
        base_max = float(getattr(S, "ob_ask_bid_max_trend" if regime=="trend_up" else ("ob_ask_bid_max_range" if regime=="range" else "ob_ask_bid_max_neutral"), 0.80))
        relax = float(getattr(S, "ob_relax_band", 0.05))
        # SHORTは ask/bid が十分大（=1/ratio が十分小）であることを確認
        ratio_inv = (1.0 / ob_ratio) if ob_ratio > 0 else float("inf")
        ob_ok = (ratio_inv <= (base_max + relax))  # ⇔ ob_ratio >= 1/(base_max+relax)
        if not ob_ok:
            # 強い売りフローで上書き
            rateS_th = float(getattr(S, "ob_override_rateS", 6000.0))
            netS_th  = float(getattr(S, "ob_override_netS", 50000.0))
            if (abs(fm_s.get("rate_usd",0.0)) >= rateS_th) and (fm_s.get("net_usd",0.0) <= -netS_th):
                relax_tags.append("ob_strong_flow_override")
            else:
                base_min_show = (1.0 / base_max) if base_max > 0 else float("inf")
                return (False, f"OB警戒: {ob_ratio:.2f} (min {base_min_show:.2f}±{relax:.2f}) "
                               f"| flowS={int(fm_s.get('rate_usd',0))}/s netS={int(fm_s.get('net_usd',0))}")

    # --- (4) Two-window flow baseline（売り優勢を確認）---
    fm_l = compute_flow_metrics(trades, int(getattr(S, "flow_window_long_sec", 60)))
    flow_ok = (
        (fm_s.get("count",0)   >= int(getattr(S, "flow_min_count", 120))) and
        (fm_s.get("consec",0)  >= int(getattr(S, "flow_min_consec",8))) and
        (fm_s.get("imbalance",0.0) <= -float(getattr(S, "flow_min_imbalance",0.25))) and
        (fm_s.get("net_usd",0.0)   <= -float(getattr(S, "flow_min_net_usd",50000.0)))
    )
    if not flow_ok:
        why = (f"flow不足: cnt={int(fm_s.get('count',0))} / consec={int(fm_s.get('consec',0))} "
               f"/ imb={float(fm_s.get('imbalance',0.0)):.2f} / netS={int(fm_s.get('net_usd',0.0))} "
               f"netL={int(fm_l.get('net_usd',0.0))}")
        if relax_tags:
            why += ' | relax=' + ','.join(relax_tags)
        return (False, why)

    # OK
    ok_msg = (f"OK | regime={regime} dist=-{dist_atr:.2f}ATR (cap {k_cap_dyn:.2f}) | "
              f"flowS(rate={int(fm_s.get('rate_usd',0))}/s net={int(fm_s.get('net_usd',0))}) "
              f"flowL(rate={int(fm_l.get('rate_usd',0))}/s net={int(fm_l.get('net_usd',0))})")
    if bool(getattr(S, "use_orderbook_filter", True)):
        ob_ratio, _, _ = compute_wall_pressure(book, int(getattr(S, 'ob_depth',50)))
        base_max = float(getattr(S, "ob_ask_bid_max_trend" if regime=="trend_up" else ("ob_ask_bid_max_range" if regime=="range" else "ob_ask_bid_max_neutral"), 0.80))
        relax = float(getattr(S, "ob_relax_band", 0.05))
        base_min_show = (1.0 / base_max) if base_max > 0 else float('inf')
        ok_msg += f" | ob={ob_ratio:.2f} (min {base_min_show:.2f}±{relax:.2f})"
    if relax_tags:
        ok_msg += " | relax=" + ",".join(relax_tags)
    return (True, ok_msg)

def _dist_atr(price: float, sma10: float, atr_val: float) -> float:
    if atr_val <= 0:
        return 0.0
    return abs(float(price) - float(sma10)) / float(atr_val)

def should_chase_breakout(ctx: dict, S) -> tuple[bool, str]:
    """
    強いブレイク（距離そこそこ大/フロー強）を“小さめ枚数”で拾うかの判定。
    戻り値: (chase_ok, note)
    """
    if not getattr(S, "use_breakout_chase", True):
        return (False, "")

    c  = float(ctx.get("price", 0))
    s10= float(ctx.get("sma10", 0))
    a  = float(ctx.get("atr", 0))
    r  = float(ctx.get("rsi", 0))
    ofi= float(ctx.get("ofi_z", 0))
    votes = int(ctx.get("edge_votes", 0))

    dist = _dist_atr(c, s10, a)
    if dist < float(getattr(S, "breakout_min_dist_atr", 1.6)):
        return (False, "")
    if dist > float(getattr(S, "breakout_max_dist_atr", 2.8)):
        return (False, "")

    if votes >= 2 and ofi >= float(getattr(S, "breakout_min_ofi_z", 2.0)) and 55.0 <= r <= 90.0:
        note = f"chase(dist_atr={dist:.2f}, ofi_z={ofi:.2f}, votes={votes})"
        return (True, note)
    return (False, "")

def is_exhaustion_long(ctx: dict, S) -> tuple[bool, str]:
    """
    吹き上がり直後の“疲労”シグナル。Trueなら LONG を一時的に禁止。
    戻り値: (blocked, reason)
    """
    if not getattr(S, "use_exhaustion_filter", True):
        return (False, "")

    c  = float(ctx.get("price", 0))
    s10= float(ctx.get("sma10", 0))
    a  = float(ctx.get("atr", 0))
    r  = float(ctx.get("rsi", 0))
    ofi= float(ctx.get("ofi_z", 0))

    dist = _dist_atr(c, s10, a)
    hard1 = dist >= float(getattr(S, "exhaustion_dist_atr", 2.5))
    hard2 = r >= float(getattr(S, "exhaustion_rsi", 85.0))
    soft  = ofi <= float(getattr(S, "exhaustion_ofi_z_min", 0.0))  # ofi_zの剥がれ（使わないなら0.0）

    # “強い距離 or RSI極端”に “ソフト条件（ofi剥がれ）”のうち1つ以上が重なればブロック
    if (hard1 or hard2) and soft:
        reason = f"exhaustion: dist_atr={dist:.2f}, RSI={r:.1f}, ofi_z={ofi:.2f}"
        return (True, reason)
    # ofi条件を使わない場合は hard1 & hard2 の両立でブロック
    if (hard1 and hard2):
        reason = f"exhaustion: dist_atr={dist:.2f}, RSI={r:.1f}"
        return (True, reason)

    return (False, "")

# レンジ上限/下限判定関数を追加
def is_range_upper(ctx: Dict[str, Any]) -> bool:
    """レンジ上限付近か判定"""
    try:
        hh = float(ctx.get("hh", 0))
        ll = float(ctx.get("ll", 0))
        price = float(ctx.get("price", 0))
        if hh <= ll:
            return False
        range_position = (price - ll) / (hh - ll)
        return range_position >= 0.7  # レンジ上位30%で上限と判定
    except:
        return False

def is_range_lower(ctx: Dict[str, Any]) -> bool:
    """レンジ下限付近か判定"""
    try:
        hh = float(ctx.get("hh", 0))
        ll = float(ctx.get("ll", 0))
        price = float(ctx.get("price", 0))
        if hh <= ll:
            return False
        range_position = (price - ll) / (hh - ll)
        return range_position <= 0.3  # レンジ下位30%で下限と判定
    except:
        return False