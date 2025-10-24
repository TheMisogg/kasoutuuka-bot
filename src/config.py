from dataclasses import dataclass
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

@dataclass(frozen=True)
class AppConfig:
    timezone: str = "Asia/Tokyo"
    poll_interval_sec: float = 5.0  # kept here too (legacy), but main.py uses S.poll_interval_sec

@dataclass(frozen=True)
class ApiConfig:
    base_url: str = "https://api.bybit.com"
    alt_hosts: tuple[str, ...] = ()

@dataclass(frozen=True)
class StrategyConfig:
    # --- Market & timeframe ---
    symbol: str = "SOLUSDT"
    category: str = "linear"
    interval_min: int = 5
    lookback_limit: int = 300
    leverage: int = 4
    

    # --- Loop / control (compat with main.py) ---
    poll_interval_sec: float = 5.0          # <== added for compatibility
    entry_cooldown_min: int = 6
    min_atr_usd: float = 0.40
    margin_ratio_stop: float = 0.50         # used by some MR guards

    # --- Exchange constraints ---
    min_notional_usdt: float = 5.10

    # --- Position sizing ---
    position_pct: float = 0.20
    max_positions: int = 4
    taker_fee_rate: float = 0.0006

    # --- Indicators ---
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    sma_fast: int = 10
    sma_slow: int = 50
    adx_period: int = 14

    # --- Regime gates ---
    atrp_trend_min: float = 0.006
    adx_trend_min: float = 16.0

    # --- Pullback ---
    entry_pullback_atr: float = 0.25
    entry_pullback_atr_trend_min: float = 0.35

    flow_window_short_sec: int = 30
    flow_window_long_sec: int = 60
    flow_min_imbalance: float = 0.25
    flow_min_count: int = 180
    flow_min_consec: int = 10
    flow_min_net_usd: float = 100000.0

    pullback_override_rateS: float = 9000.0
    pullback_override_netS: float = 80000.0

    # --- Distance hard-cap from SMA10 ---
    entry_max_over_sma10_atr_trend: float = 1.50
    entry_max_over_sma10_atr_neutral: float = 0.70
    entry_max_over_sma10_atr_range: float = 0.55

            # --- 動的キャップ／モメンタム緩和（新規） ---
    cap_bonus_enabled: bool = True
    cap_bonus_votes2: float = 0.05        # votes=2 のときの基本加点
    cap_bonus_per_vote: float = 0.08      # votes>=3 での 1 票あたり加点
    ofi_z_boost_thr: float = 2.0          # OFI z 開始しきい値
    cap_bonus_ofi_z_k: float = 0.06       # z 超過 1.0 あたりの加点
    cap_bonus_neutral_max: float = 0.45   # neutral 上限
    cap_bonus_range_max: float = 0.30     # range 上限
    use_momentum_pullback_override: bool = True
    momentum_votes_min: int = 2           # これ以上の votes で押し目待ちを上書き可

    # --- Orderbook soft-guard ---
    use_orderbook_filter: bool = True
    ob_depth: int = 50
    wall_scan_atr_k: float = 0.5
    ob_ask_bid_max_trend: float = 1.40
    ob_ask_bid_max_neutral: float = 0.93
    ob_ask_bid_max_range: float = 0.80
    ob_relax_band: float = 0.08
    ob_override_rateS: float = 6000.0
    ob_override_netS: float = 50000.0

    # --- Move to BE ---
    use_move_to_be = False
    move_be_atr_k_trend: float = 1.10
    move_be_atr_k_neutral: float = 1.00
    move_be_atr_k_range: float = 0.70

    # --- 初期SL/TP（ATR基準・RR一定）---
    sl_atr_k_trend   = 1.35
    sl_atr_k_neutral = 1.20
    sl_atr_k_range   = 1.00
    tp_rr            = 1.8          # RR=1.8x（後で2.0に上げてもOK）
    min_sl_usd       = 0.20    

    # --- 距離キャップ（“SMA10から遠すぎる”の判定を少し緩める）---
    dist_cap_base: float = 0.95          # 0.85 → 0.95
    dist_cap_bonus_max: float = 0.40     # 緩和上限 +0.30 → +0.40

    # --- 追いモード（ブレイクアウト・チェイス）---
    use_breakout_chase: bool = True
    breakout_min_dist_atr: float = 1.6   # SMA10からの距離がこの倍以上かつ…
    breakout_max_dist_atr: float = 2.8   # …この倍以下ならチェイス許可帯
    breakout_min_ofi_z: float = 2.0
    breakout_half_size: float = 0.5      # 通常の 50% サイズ
    breakout_sl_k: float = 1.6           # SLは少し広め（加速押しで刈られにくく）
    breakout_time_stop_min: int = 3      # 約定後 n 分以内に follow_through_R に届かねば撤退
    breakout_follow_through_R: float = 0.6

    # --- 吹き上がり直後の“疲労”フィルター（ロング抑制）---
    use_exhaustion_filter: bool = True
    exhaustion_dist_atr: float = 2.5     # 距離が大きすぎ
    exhaustion_rsi: float = 85.0         # RSI 極端
    exhaustion_ofi_z_min: float = 0.0    # ofi_z が低い/剥がれ（閾値未使用なら 0.0 のまま）
    exhaustion_block_bars: int = 2       # n 本（5m×n）ロング禁止

    momentum_extra_atr_neutral: float = 0.30

    use_pivot_ob_override: bool = True
    pivot_max_dist_atr: float = 1.20
    pivot_ob_max_ratio: float = 0.75  # ask/bid
    pivot_min_ofi_z: float = 1.2

    # ===== Range-Top Guard（レンジ上でロング抑制）=====
    range_lookback: int = 60                  # 5m×60 ≒ 5時間レンジ
    range_top_pos: float = 0.70               # [0..1] 上位ゾーンしきい値
    range_top_ask_bid_min: float = 1.05       # ask/bid ≥ → 天井リスク
    range_top_ofi_z_max: float = 0.30         # OFI z が弱いときは見送り

    # ===== OB “持続”フィルタ（瞬間偏りを弾く）=====
    ob_hist_len: int = 6                      # 直近Nサンプルで平均
    ob_persist_ask_bid_min: float = 1.08      # 平均 ask/bid ≥ で強警戒

    # --- microstructure gating ---
    required_votes_min: int = 3          # ← 2→3 に上げる（弱い合意を弾く）
    ofi_z_entry_min: float = 1.5         # 同方向通常エントリーの下限
    ofi_z_entry_min_strong: float = 2.4  # カウンタートレンド時はこの強度以上を要求
    cons_buy_min: int = 3                # 連続買い/売り（通常）
    cons_sell_min: int = 3
    cons_buy_min_strong: int = 4         # カウンタートレンドは 4 以上
    cons_sell_min_strong: int = 4
    net_mkt_usd_min: float = 8000.0      # 30〜60秒窓の符号付き成行フロー下限（通常）
    net_mkt_usd_min_strong: float = 12000.0  # カウンタートレンド時はより大きく
    cvd_slope_min: float = 0.0           # 使っていれば。未使用なら 0.0 でOK

    # --- regime aware counter-trend guard ---
    block_countertrend_if_rsi_gt: float = 60.0     # RSIが高い上昇局面でのショートは厳格化
    block_countertrend_if_dist_atr_lt: float = 0.2 # SMA10からの距離が小さい時は見送り

    # --- relax/widen control ---
    disable_trend_widen_for_counter: bool = True   # 逆張りでは widen を無効化

    # --- flip (反転) 例外: クールダウン無視で即反転を許可 ---
    flip_cooldown_override: bool = True
    flip_ofi_z_min: float = 2.2                    # 強い逆方向フロー
    flip_cons_min: int = 5                         # 逆方向の連続約定が十分
    flip_break_sma10: bool = True                  # 逆方向へ SMA10 を跨いだらOK
    flip_time_window_sec: float = 120.0            # 直近N秒以内の強フロー

    # 強トレンド & フロー合致の判定
    trend_votes_min = 2
    trend_ofi_z_min = 1.5

    # Trend strong
    sl_trend_long_atr  = 1.2
    tp_rr_trend_long   = 2.0
    be_k_trend_long    = 0.6
    sl_trend_short_atr = 1.3   # ← 非対称（上昇トレンドでのショートは広め）
    tp_rr_trend_short  = 2.0
    be_k_trend_short   = 0.6

    # Range
    sl_range_atr  = 0.9
    tp_rr_range   = 1.6
    trail_k_range = 0.30

    # Neutral
    sl_neutral_atr = 1.1
    tp_rr_neutral  = 1.8
    be_k_neutral   = 0.5

    # クールダウン override の閾値（厳しめ）
    cooldown_override_enable = True
    cooldown_ofi_z_min = 3.2
    cooldown_cons_min  = 10
    cooldown_cvd_z_min = 1.8
    cooldown_adx_min   = 22.0
    cooldown_override_min_gap_sec = 900   # 15分
    cooldown_override_max_per_hour = 4
    # --- main.py が参照する“別名”をマッピング ---
    cooldown_override_ofi_z = cooldown_ofi_z_min
    cooldown_override_cons  = cooldown_cons_min
    regime_override_ofi_z   = cooldown_ofi_z_min   # まずは同値でOK（別にしたければ分けても可）
    regime_override_cons    = cooldown_cons_min
        # --- Cooldown override / regime override 用の票数しきい値 ---
    cooldown_override_votes: int = 5   # ← 推奨、まずは厳しめ
    regime_override_votes: int = 5     # ← 互換エイリアス（どちらか片方だけでも可）
    cooldown_override_adx_min = 22.0
    # 強フロー解除の最小ATR%（低ボラのレンジでは解除しない）
    override_min_atr_pct: float = 0.004
    # OFI z の上限クリップ（σ）
    ofi_z_clip: float = 6.0

    # --- debug ---
    debug_flow: bool = True  # OFI z などのフロー系デバッグをSlackへ出す

    allow_atomic_flip = False         # 既定は反転しない
    min_hold_minutes_after_entry = 5  # 建ててから最低5分は反転しない
    min_flip_interval_min = 10        # 反転→反転の最短間隔
    flip_enable = True                # 有効でも、allow_atomic_flip=Falseなら発火しない
    flip_votes_needed = 2
    flip_ofi_z = 3.0
    flip_cons = 5
    flip_cvd_z = 2.0

    use_postonly_entries = True

        # RSI conditions
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    # Multi-timeframe trend
    use_1h_trend: bool = True
    use_1h_trend_confirmation: bool = True  # この行を追加
    trend_sma_fast: int = 50
    trend_sma_slow: int = 200

    # Volatility filter
    use_atr_filter: bool = True
    atr_ratio_min: float = 0.5  # 現在のATRが平均ATRの何倍以下ならボラティリティ不足とみなす

    # Regime-based entry control
    range_upper_bound_percentile: float = 0.7  # レンジ上限（0.7は例）
    range_lower_bound_percentile: float = 0.3  # レンジ下限


APP = AppConfig()
API = ApiConfig()
STRATEGY = StrategyConfig()