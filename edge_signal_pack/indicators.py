from collections import deque
import numpy as np
import pandas as pd


def _rolling_robust_z(x: np.ndarray, win: int, clip: float = 6.0) -> float:
    w = x[-win:]
    med = np.median(w); mad = np.median(np.abs(w - med))
    if mad <= 1e-6: return 0.0
    z = 0.6745 * (w[-1] - med) / mad
    return float(np.clip(z, -clip, clip))

# --- robust z (MAD) helper ---
def _robust_z_last(x: np.ndarray, win: int = 120, min_samples: int = 60, clip: float = 6.0) -> float:
    """
    直近 win 本の系列から、最後の値のロバスト z を返す（MAD使用）。
    データ不足や MAD 極小時は 0。最終的に ±clip にクリップ。
    """
    if x is None:
        return 0.0
    n = len(x)
    if n < max(win, min_samples):
        return 0.0
    w = np.asarray(x[-win:], dtype=float)
    med = np.median(w)
    mad = np.median(np.abs(w - med))
    if mad < 1e-6:
        return 0.0
    z = 0.6745 * (w[-1] - med) / mad
    return float(np.clip(z, -clip, clip))

# -------- OBI --------
def order_book_imbalance(bids, asks, levels=8):
    # bids/asks: list of [price, size] sorted
    b = sum(x[1] for x in bids[:levels])
    a = sum(x[1] for x in asks[:levels])
    if b+a == 0:
        return 0.0
    return (b - a) / (b + a)

# -------- OFI / CVD --------
class FlowBuckets:
    def __init__(self, window_sec: int = 3600, z_clip: float = 6.0):
        self.window_sec = int(window_sec)
        self.z_clip = float(z_clip)
        self.buckets: list[tuple[int, float, float]] = []
        # --- debug counters ---
        self._dbg_ofi_len: int = 0
        self._dbg_ofi_win: int = 0
        self._dbg_seen: int = 0       # 受け取った tick 数
        self._dbg_added: int = 0   

    def add_trade(self, ts: int, side: str, qty: float) -> None:
    # 受信カウント（まずは seen を増やす）
        self._dbg_seen += 1

        # side の頑健化
        try:
            s = str(side).lower()
            is_buy = s.startswith("b") or (s == "buy") or (s == "1")
        except Exception:
            is_buy = False

        # qty
        try:
            q = float(qty or 0.0)
        except Exception:
            q = 0.0
        if q <= 0:
            return  # ここで積まない

        # ts → 秒
        try:
            ts_sec = int(float(ts)) // 1000
        except Exception:
            return  # ここで積まない

        # ここまで来たら積めるので added++
        self._dbg_added += 1

        # 末尾秒と同じなら加算、違えば新設
        if self.buckets and self.buckets[-1][0] == ts_sec:
            t, b, s_ = self.buckets[-1]
            if is_buy: b += q
            else:      s_ += q
            self.buckets[-1] = (t, b, s_)
        else:
            self.buckets.append((ts_sec, q if is_buy else 0.0, 0.0 if is_buy else q))

        # 古いものを window から落とす
        cutoff = ts_sec - self.window_sec
        i = 0
        for i in range(len(self.buckets)):
            if self.buckets[i][0] >= cutoff:
                break
        if i > 0:
            self.buckets = self.buckets[i:]

    def ofi_series(self) -> list[float]:
        """各秒バケットの (buy - sell) 列"""
        if not self.buckets:
            return []
        return [b - s for (_ts, b, s) in self.buckets]

    def ofi_zscore(self) -> float:
        """ロバストZ（Median/MAD、クリップ付き）"""
        x = self.ofi_series()
        n = len(x)

        # ← 先に debug を更新（早期 return 前）
        win = 30 if n < 30 else 60 if n < 60 else 120
        self._dbg_ofi_len = n
        self._dbg_ofi_win = win

        if n < 5:
            return 0.0

        arr = np.asarray(x[-win:], dtype=float)
        med = np.median(arr)
        mad = np.median(np.abs(arr - med))
        if mad < 1e-6:
            return 0.0

        z = 0.6745 * (arr[-1] - med) / mad
        return float(np.clip(z, -self.z_clip, self.z_clip))

class CVDTracker:
    def __init__(self, ema_period=20):
        self.value = 0.0
        self.alpha = 2/(ema_period+1)
        self.ema = 0.0
        self._ema_init = False
        self.seq_mkt_buys = 0
        self.seq_mkt_sells = 0
        self.last_sec = None

    def on_trade(self, ts_ms: int, side: str, qty: float):
        # +qty for buy market, -qty for sell market
        delta = qty if side.lower().startswith("b") else -qty
        self.value += delta
        # simple EMA on value
        if not self._ema_init:
            self.ema = self.value
            self._ema_init = True
        else:
            self.ema += self.alpha * (self.value - self.ema)

        # 1秒足の連続カウント
        sec = ts_ms // 1000
        if self.last_sec is None or sec != self.last_sec:
            # 新しい秒に入ったらカウントリセット
            self.seq_mkt_buys = 0
            self.seq_mkt_sells = 0
            self.last_sec = sec
        if delta > 0:
            self.seq_mkt_buys += 1
            self.seq_mkt_sells = 0
        elif delta < 0:
            self.seq_mkt_sells += 1
            self.seq_mkt_buys = 0

    @property
    def slope_positive(self):
        return self.value > self.ema

# -------- ATR% / ADX（簡易版） --------
def atr_percent(df: pd.DataFrame, period: int = 14):
    # df: columns ['open','high','low','close']
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    tr = np.maximum(high[1:], close[:-1]) - np.minimum(low[1:], close[:-1])
    atr = pd.Series(tr).rolling(period).mean().iloc[-1]
    price = close[-1]
    return float(atr / price) if price else 0.0

def adx(df: pd.DataFrame, period: int = 14):
    # 簡易ADX
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    up = high[1:] - high[:-1]
    dn = low[:-1] - low[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0)
    tr = np.maximum(high[1:], close[:-1]) - np.minimum(low[1:], close[:-1])
    atr = pd.Series(tr).rolling(period).mean()
    pdi = 100 * (pd.Series(plus_dm).rolling(period).mean() / atr)
    mdi = 100 * (pd.Series(minus_dm).rolling(period).mean() / atr)
    dx = 100 * (abs(pdi - mdi) / (pdi + mdi))
    return float(dx.rolling(period).mean().iloc[-1])
