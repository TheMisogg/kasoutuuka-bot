
from typing import List, Tuple

def ema(values: List[float], span: int) -> List[float]:
    if not values:
        return []
    k = 2 / (span + 1)
    out: List[float] = []
    ema_val = None
    for v in values:
        if ema_val is None:
            ema_val = v
        else:
            ema_val = (v - ema_val) * k + ema_val
        out.append(ema_val)
    return out

def sma(values: List[float], window: int) -> List[float]:
    out: List[float] = []
    buf: List[float] = []
    for v in values:
        buf.append(v)
        if len(buf) > window:
            buf.pop(0)
        if len(buf) == window:
            out.append(sum(buf)/window)
        else:
            out.append(float("nan"))
    return out

def rsi(values: List[float], period: int = 14) -> List[float]:
    rsis = [float("nan")] * len(values)
    gains: List[float] = [0.0]
    losses: List[float] = [0.0]
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    if len(values) > period:
        avg_gain = sum(gains[1:period + 1]) / period
        avg_loss = sum(losses[1:period + 1]) / period
        for i in range(period + 1, len(values)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            rs = float("inf") if avg_loss == 0 else (avg_gain / avg_loss)
            rsis[i] = 100 - (100 / (1 + rs))
    return rsis

def macd(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[List[float], List[float], List[float]]:
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist

def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    trs: List[float] = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            trs.append(tr)
    at = [float("nan")] * len(trs)
    if len(trs) >= period:
        s = sum(trs[:period])
        at[period - 1] = s / period
        for i in range(period, len(trs)):
            s = s - trs[i - period] + trs[i]
            at[i] = s / period
    return at
