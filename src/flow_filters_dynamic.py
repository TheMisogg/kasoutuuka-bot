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
from typing import Any, Dict, Tuple

# ===============================================================
# Guard用ヘルパ（代替MA選択とATRバッファ計算）
# ===============================================================
def _get_guard_ma_and_name(ctx: Dict[str, Any], s10_fallback: float, S=S) -> Tuple[float, str]:
    """
    ガードで使うMAを選択（SMA10|SMA20|EMA10）。ctxに無ければSMA10へフォールバック。
    戻り値: (ma_value, display_name)
    """
    t = str(getattr(S, "guard_ma_type", "SMA10")).upper()
    if t == "SMA20":
        val = float(ctx.get("sma20", s10_fallback))
        return val, "SMA20"
    if t == "EMA10":
        # ema10 が無ければ ema9 → sma10 の順にフォールバック
        val = float(ctx.get("ema10", ctx.get("ema9", s10_fallback)))
        return val, "EMA10"
    # 既定：SMA10
    return float(ctx.get("sma10", s10_fallback)), "SMA10"

def _calc_guard_buffer_k(regime: str, atr: float, price: float, S=S) -> float:
    """
    ガードのATRバッファ係数 k を算出（レジーム倍率＋ATR%に基づく動的拡張）。
    k の単位は“ATR倍率”。最終的な価格幅は k * ATR。
    """
    base = float(getattr(S, "guard_buffer_atr_base", 0.10))
    # レジーム倍率
    if regime in ("trend_up", "trend_down"):
        mul = float(getattr(S, "guard_buffer_mul_trend", 1.00))
    elif regime == "range":
        mul = float(getattr(S, "guard_buffer_mul_range", 0.80))
    else:
        mul = float(getattr(S, "guard_buffer_mul_neutral", 0.60))

    k = base * mul
    # ATR%で動的拡大（高ボラほど緩め、低ボラは基準のまま）
    if bool(getattr(S, "use_dynamic_buffer_by_atrp", True)) and price > 0.0 and atr >= 0.0:
        atrp = atr / price
        ref  = float(getattr(S, "guard_buffer_atrp_ref", 0.010))
        slope= float(getattr(S, "guard_buffer_atrp_slope", 1.0))
        if atrp > ref and ref > 0:
            k *= (1.0 + slope * (atrp - ref) / ref)
    # 上限キャップ
    k_cap = float(getattr(S, "guard_buffer_atr_cap", 0.30))
    if k > k_cap:
        k = k_cap
    if k < 0.0:
        k = 0.0
    return k
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
    """
    市況レジームを4分類で返す:
      - "trend_up" / "trend_down" / "range" / "neutral"
    併せて、マルチタイムフレーム整合や“強さスコア”を ctx にメタ情報として格納する。
    （戻り値はレジーム名のみ。強トレンドかどうかの最終判断は main.py 側で合成）
    """
    # --- 安全取得 ---
    def _g(name: str, default=0.0, t=float):
        try:
            v = ctx.get(name, default)
            return t(v)
        except Exception:
            return t(default)

    price = _g("price", 0.0) or 1.0
    atr   = _g("atr", 0.0)
    adx   = _g("adx", 0.0)  # 5m
    s10   = _g("sma10", _g("ema9", price))
    s50   = _g("sma50", _g("ema21", s10))
    macd  = _g("macd", 0.0)
    msig  = _g("macd_sig", 0.0)
    rsi   = _g("rsi14", _g("rsi", 50.0))
    atrp  = (atr / price) if price else 0.0

        # === 追加1: 流動性確認（最初にチェック）===
    volume_24h_avg = _g("volume_24h_avg", 0.0)
    current_volume = _g("volume", _g("vol", 0.0))
    low_liquidity = (volume_24h_avg > 0 and 
                    current_volume < volume_24h_avg * 0.3)
    
    if low_liquidity:
        ctx["low_liquidity"] = True
        ctx["mtf_align"] = "none"
        ctx["strong_score_up"] = 0
        ctx["strong_score_down"] = 0
        return "neutral"  # 流動性低下時は中立判定

    # --- まず「レンジ」を先判定（ボラ小&短長MA収束） ---
    rng_atrp_max      = float(getattr(S, "atrp_range_max", 0.008))
    sma_conf_atr_mult = float(getattr(S, "sma_confluence_atr_k", 0.30))  # |s10-s50| <= k*ATR
    if (atrp <= rng_atrp_max) and (abs(s10 - s50) <= (sma_conf_atr_mult * max(atr, 1e-9))):
        ctx["mtf_align"] = "none"
        ctx["strong_score_up"] = 0
        ctx["strong_score_down"] = 0
        return "range"

    # === 追加2: 急激な価格変動の検出 ===
    price_5m_ago = _g("price_5m_ago", _g("price_prev", price))
    price_change_5m = abs(price - price_5m_ago) / (price_5m_ago or 1.0)
    sudden_move = price_change_5m > 0.03  # 5分で3%以上
    
    if sudden_move:
        ctx["sudden_move"] = True
        ctx["price_change_5m"] = price_change_5m

    # --- MTF整合（5m × 15m/1h）: EMA9/EMA21 + ADX ---
    adx_5m_min  = float(getattr(S, "adx_5m_min", 25.0))
    adx_15m_min = float(getattr(S, "adx_15m_min", 20.0))
    adx_1h_min  = float(getattr(S, "adx_1h_min", 18.0))

    ema9_5  = _g("ema9",  _g("sma10", s10))
    ema21_5 = _g("ema21", _g("sma50", s50))
    ema9_15 = _g("ema9_15m",  _g("sma10_15m", ema9_5))
    ema21_15= _g("ema21_15m", _g("sma50_15m", ema21_5))
    ema9_1h = _g("ema9_1h",   _g("sma10_1h",  ema9_5))
    ema21_1h= _g("ema21_1h",  _g("sma50_1h",  ema21_5))
    adx_15  = _g("adx_15m", _g("adx15", 0.0))
    adx_1h  = _g("adx_1h",  _g("adx60", 0.0))

    m5_up   = (adx >= adx_5m_min)  and (ema9_5  > ema21_5)
    m5_down = (adx >= adx_5m_min)  and (ema9_5  < ema21_5)
    m15_up  = (adx_15 >= adx_15m_min) and (ema9_15 > ema21_15)
    m15_down= (adx_15 >= adx_15m_min) and (ema9_15 < ema21_15)
    h1_up   = (adx_1h >= adx_1h_min)   and (ema9_1h > ema21_1h)
    h1_down = (adx_1h >= adx_1h_min)   and (ema9_1h < ema21_1h)

    align_up   = m5_up   and (m15_up or h1_up)
    align_down = m5_down and (m15_down or h1_down)
    ctx["mtf_align"] = "up" if align_up else ("down" if align_down else "none")

    # --- “強さスコア”（上昇/下降を別々に採点） ---
    # 使える値が無ければ各条件はFalse扱い（堅牢化）
    # MA整列: ema_fast > ema_mid > ema_slow（無ければ fast>mid 判定のみ）
    ema_fast = ema9_5
    ema_mid  = ema21_5
    ema_slow = _g("ema50", _g("sma200", ema_mid + 1.0))
    have_slow= ("ema50" in ctx) or ("sma200" in ctx)
    ma_up    = (ema_fast > ema_mid) and (ema_mid > ema_slow if have_slow else True)
    ma_down  = (ema_fast < ema_mid) and (ema_mid < ema_slow if have_slow else True)

    adx_strong = adx >= float(getattr(S, "adx_strong_min", 23.0))
    vol        = _g("volume", _g("vol", 0.0))
    vol_ma     = _g("vol_ma", _g("volume_ma", 0.0))
    vol_exp    = (vol_ma > 0.0) and (vol > vol_ma * float(getattr(S, "volume_expand_k", 1.3)))
    bbw        = _g("bb_width", _g("bb_w", 0.0))
    bbw_ma     = _g("bb_width_ma", _g("bb_w_ma", 0.0))
    bb_expand  = (bbw_ma > 0.0) and (bbw > bbw_ma)
    mh         = _g("macd_hist", _g("macd_histogram", 0.0))
    mh_prev    = _g("macd_hist_prev", _g("macd_hist_1", mh))
    mh_up      = (mh > 0.0) and (mh >= mh_prev)
    mh_down    = (mh < 0.0) and (mh <= mh_prev)

    rsi_up     = (rsi >= float(getattr(S, "rsi_strong_min", 60.0))) and (rsi <= float(getattr(S, "rsi_strong_max", 80.0)))
    rsi_down   = (rsi <= float(getattr(S, "rsi_weak_max", 40.0)))   and (rsi >= float(getattr(S, "rsi_weak_min", 20.0)))

    strong_score_up   = int(adx_strong) + int(ma_up)   + int(rsi_up)   + int(vol_exp) + int(bb_expand) + int(mh_up)
    strong_score_down = int(adx_strong) + int(ma_down) + int(rsi_down) + int(vol_exp) + int(bb_expand) + int(mh_down)
    ctx["strong_score_up"] = strong_score_up
    ctx["strong_score_down"] = strong_score_down

    # === 追加3: 急激な価格変動がある場合は強さスコアを補正 ===
    if sudden_move:
        # 急変時はより保守的な判定 - 既存の強さスコアに重みを追加
        strong_score_up = min(strong_score_up + 1, 6)
        strong_score_down = min(strong_score_down + 1, 6)

    ctx["strong_score_up"] = strong_score_up
    ctx["strong_score_down"] = strong_score_down

    # === 追加4: 仮想通貨相関指標 ===
    btc_correlation = _g("btc_correlation", 1.0)
    if abs(btc_correlation) > 0.7:
        # BTCと強く連動している場合、信頼度を調整
        ctx["high_correlation"] = True
        ctx["btc_correlation_value"] = btc_correlation

    # --- トレンド“ゲート”: ATR%/ADX のモード切替（OR/AND/ADX/ATR） ---
    mode     = str(getattr(S, "trend_gate_mode", "OR")).upper()
    atr_gate = atrp >= float(getattr(S, "atrp_trend_min", 0.012))
    adx_gate = adx  >= float(getattr(S, "adx_trend_min", 20.0))
    if mode == "AND":
        gate_trend = atr_gate and adx_gate
    elif mode == "ADX":
        gate_trend = adx_gate
    elif mode == "ATR":
        gate_trend = atr_gate
    else:  # "OR"
        gate_trend = atr_gate or adx_gate

    # --- 方向確定（MTF整合があれば優先、無ければ5mのMA+MACD整合） ---
    up_dir   = (s10 > s50 and macd >= msig) or align_up
    down_dir = (s10 < s50 and macd <= msig) or align_down

    if gate_trend:
        if up_dir and not down_dir:
            return "trend_up"
        if down_dir and not up_dir:
            return "trend_down"
    
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

def should_allow_override(regime: str, side: str, flow_metrics: Dict | None = None) -> bool:
    """
    レジームに応じて「強フロー等のオーバーライド」を許可するか判定。
    - trend_up  : LONG のみ許可
    - trend_down: SHORT のみ許可
    - neutral / range: 両方向許可
    flow_metrics は将来拡張用（現状未使用）。
    """
    side = (side or "").upper()
    if regime == "trend_up" and side != "LONG":
        return False
    if regime == "trend_down" and side != "SHORT":
        return False
    return True

def decide_entry_guard_long(trades: list, book: dict, ctx: Dict[str, Any], S=S) -> Tuple[bool, str]:
    price = float(ctx.get("price", 0.0))
    s10   = float(ctx.get("sma10", price))
    s50   = float(ctx.get("sma50", s10))
    atr   = float(ctx.get("atr", 0.0)) or 1e-9
    rsi   = float(ctx.get("rsi", ctx.get("rsi14", 50.0)))
    # --- Regime classify (Slack 表示用にガード前で設定) ---
    regime = classify_regime(ctx) # "trend_up" / "trend_down" / "range" / "neutral"
    ctx["regime"] = regime

    # --- ガード（代替MA + ATRバッファ） ---
    #   目的：SMA跨ぎの微細ノイズを許容（長期トレンド継続時の誤ブロックを減らす）
    #   仕様：LONGは price >= MA - k*ATR を満たせば通過（kはレジーム/ATR%で動的）
    gma, gname = _get_guard_ma_and_name(ctx, s10, S)
    k_guard = _calc_guard_buffer_k(regime, atr, price, S)
    if getattr(S, "require_close_gt_sma10_long", True):
        # trend_up でガード無効にする設定ならスキップ（他ガードは後続で評価）
        if not (regime == "trend_up" and bool(getattr(S, "guard_disable_long_in_trend_up", False))):
            thr = gma - (k_guard * atr)
            if price < thr:
                return (False, f"close<{gname}-{k_guard:.2f}ATR(guard)")
    if rsi < float(getattr(S, "rsi_long_min", 55.0)):
        return (False, f"RSI<{int(getattr(S, 'rsi_long_min', 55))}(guard)")    

    relax_tags = []

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
    k_cap_base = k_trend if regime in ("trend_up", "trend_down") else (k_range if regime == "range" else k_neutral)

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
        regime_ok = should_allow_override(regime, "LONG", fm_s)
        if allow_by_flow and regime_ok:
            relax_tags.append("strong_flow_override")
        elif allow_by_momentum and regime_ok:
            relax_tags.append("momentum_override")
        else:
           # --- Pivot-OB override: SMA10 近辺で bid 優勢なら押し目成立とみなす ---
           if bool(getattr(S, "use_pivot_ob_override", True)):
               ob_ratio, _, _ = compute_wall_pressure(book, int(getattr(S, "ob_depth",50)))
               pivot_max_dist = float(getattr(S, "pivot_max_dist_atr", 1.20))   # 目安: ≤1.2ATR上
               want =  float(getattr(S, "pivot_ob_max_ratio", 0.75))            # ask/bid ≤ 0.75（bid優勢）
               need_z = float(getattr(S, "pivot_min_ofi_z", 1.2))               # 最低限のOFI z
               if regime_ok and (dist_atr <= pivot_max_dist) and (ob_ratio <= want) and (ofi_z >= need_z) and (votes >= 2):
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
            if (fm_s.get("rate_usd",0.0) >= rateS_th) and (fm_s.get("net_usd",0.0) >= netS_th) \
               and should_allow_override(regime, "LONG", fm_s):
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
    # --- Regime classify (Slack 表示用にガード前で設定) ---
    regime = classify_regime(ctx) # "trend_up" / "trend_down" / "range" / "neutral"
    ctx["regime"] = regime
    # --- ガード（代替MA + ATRバッファ） ---
    #   仕様：SHORTは price <= MA + k*ATR を満たせば通過
    gma, gname = _get_guard_ma_and_name(ctx, s10, S)
    k_guard = _calc_guard_buffer_k(regime, atr, price, S)
    if getattr(S, "require_close_lt_sma10_short", True):
        # trend_down ではショート側を緩和／無効化（設定で切替）
        if not (regime == "trend_down" and bool(getattr(S, "guard_disable_short_in_trend_down", True))):
            thr = gma + (k_guard * atr)
            if price > thr:
                return (False, f"close>{gname}+{k_guard:.2f}ATR(guard)")
    if rsi > float(getattr(S, "rsi_short_max", 50.0)):
        return (False, f"RSI>{int(getattr(S, 'rsi_short_max', 50))}(guard)")

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
    k_cap_base = k_trend if regime in ("trend_up", "trend_down") else (k_range if regime == "range" else k_neutral)

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
        regime_ok = should_allow_override(regime, "SHORT", fm_s)
        if allow_by_flow and regime_ok:
            relax_tags.append("strong_flow_override")
        elif allow_by_momentum and regime_ok:
            relax_tags.append("momentum_override")
        else:
            # --- Pivot-OB override: SMA10近辺で ask 優勢（頭上の売り板）なら戻り売り成立 ---
            if bool(getattr(S, "use_pivot_ob_override", True)):
                ob_ratio, _, _ = compute_wall_pressure(book, int(getattr(S, "ob_depth",50)))
                pivot_max_dist = float(getattr(S, "pivot_max_dist_atr", 1.20))   # 目安: ≤1.2ATR下
                want_min = 1.0 / max(1e-9, float(getattr(S, "pivot_ob_max_ratio", 0.75)))  # ask/bid ≥ 1/(0.75)=1.33...
                need_z   = float(getattr(S, "pivot_min_ofi_z", 1.2))
                if regime_ok and (dist_atr <= pivot_max_dist) and (ob_ratio >= want_min) and (ofi_z <= -need_z) and (votes >= 2):
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
        base_max = float(getattr(S, "ob_ask_bid_max_trend" if regime in ("trend_up","trend_down") else ("ob_ask_bid_max_range" if regime=="range" else "ob_ask_bid_max_neutral"), 0.80))
        relax = float(getattr(S, "ob_relax_band", 0.05))
        # SHORTは ask/bid が十分大（=1/ratio が十分小）であることを確認
        ratio_inv = (1.0 / ob_ratio) if ob_ratio > 0 else float("inf")
        ob_ok = (ratio_inv <= (base_max + relax))  # ⇔ ob_ratio >= 1/(base_max+relax)
        if not ob_ok:
            # 強い売りフローで上書き
            rateS_th = float(getattr(S, "ob_override_rateS", 6000.0))
            netS_th  = float(getattr(S, "ob_override_netS", 50000.0))
            if (abs(fm_s.get("rate_usd",0.0)) >= rateS_th) and (fm_s.get("net_usd",0.0) <= -netS_th) \
               and should_allow_override(regime, "SHORT", fm_s):
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
        base_max = float(getattr(S, "ob_ask_bid_max_trend" if regime in ("trend_up","trend_down") else ("ob_ask_bid_max_range" if regime=="range" else "ob_ask_bid_max_neutral"), 0.80))
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