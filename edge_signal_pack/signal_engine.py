import threading, time, datetime as dt
from typing import Optional, Dict, Any
import pandas as pd

from . import config
from .bybit_ws import BybitWS
from .bybit_rest import market_open_interest
from .indicators import (
    order_book_imbalance, FlowBuckets, CVDTracker,
    atr_percent, adx
)
from .strategy import decide_signal, has_liq_cluster

class EdgeSignalEngine:
    def __init__(self, symbol="SOLUSDT", timeframe_base="5m", jst_active_hours=((16,0,0),(2,0,0))):
        self.symbol = symbol
        self.timeframe_base = timeframe_base
        self.jst_active_hours = jst_active_hours

        self.ws = BybitWS(symbol)
        self.flow = FlowBuckets(window_sec=config.OFI_WINDOW_SEC)
        self.cvd = CVDTracker(ema_period=config.CVD_EMA)

        self._regime_ok = False
        self._doi_up_ok: Optional[bool] = None
        self._price = None

        self._lock = threading.Lock()
        self.last_reasons = []
        self._metrics = {}

    # ---- lifecycle ----
    def start(self):
        self.ws.start()
        # OI polling thread
        if config.DOI_USE:
            t = threading.Thread(target=self._poll_oi_loop, daemon=True)
            t.start()
        # metrics update thread
        t2 = threading.Thread(target=self._metrics_loop, daemon=True)
        t2.start()

    # ---- helpers ----
    def _vote_count(self) -> int:
        """
        現在のメトリクスから方向を問わない票数（強フロー度合い）を概算する。
        EdgeSignal の最終判定と独立に、OBI/OFI/CVD/清算/ΔOI を加点方式で数える。
        """
        m: Dict[str, Any] = {}
        try:
            m = dict(self._metrics)  # snapshot
        except Exception:
            m = {}
        votes = 0
        # OBI（板不均衡）：絶対値で判定
        try:
            if abs(float(m.get("obi", 0.0))) >= float(config.OBI_THR):
                votes += 1
        except Exception:
            pass
        # OFI（約定フロー不均衡）：絶対値で判定
        try:
            if abs(float(m.get("ofi_z", 0.0))) >= float(config.OFI_Z_THR):
                votes += 1
        except Exception:
            pass
        # CVD（出来高累積の傾き）× 連続ティック
        try:
            seq_buys  = int(m.get("seq_buys", 0))
            seq_sells = int(m.get("seq_sells", 0))
            if bool(m.get("cvd_above_ema", False)):
                if seq_buys  >= int(getattr(config, "SEQ_MKT_TICKS", 3)): votes += 1
            else:
                if seq_sells >= int(getattr(config, "SEQ_MKT_TICKS", 3)): votes += 1
        except Exception:
            pass
        # 清算クラスター
        if m.get("liq_cluster_ok", None) is True:
            votes += 1
        # ΔOI（オープンインタレスト増）
        if m.get("doi_up_ok", None) is True:
            votes += 1
        return votes


    def _poll_oi_loop(self):
        last_oi = None
        while True:
            try:
                lst = market_open_interest(self.symbol, interval="5min")
                if lst:
                    # 最新2点で比較（簡易）
                    cur = float(lst[0].get("openInterest", 0) or 0.0)
                    if last_oi is not None and last_oi > 0:
                        pct = (cur - last_oi)/last_oi
                        self._doi_up_ok = (pct >= config.DOI_5M_PCT)
                    last_oi = cur
            except Exception:
                pass
            time.sleep(60)

    def _metrics_loop(self):
        while True:
            try:
                # price
                ob = self.ws.snapshot_orderbook()
                bids, asks = ob["bids"], ob["asks"]
                if bids and asks:
                    best_bid, best_ask = bids[0][0], asks[0][0]
                    self._price = (best_bid + best_ask)/2.0

                # OBI
                obi = 0.0
                if bids and asks:
                    obi = order_book_imbalance(bids, asks, levels=config.OBI_LEVELS)

                # Trades -> OFI/CVD
                trades = self.ws.snapshot_trades()

                def _get(d, *keys, default=None):
                    for k in keys:
                        if k in d and d[k] is not None:
                            return d[k]
                    return default

                added = 0
                seen  = len(trades)

                for d in trades[-50:]:
                    ts_raw = _get(d, "ts", "T", "trade_time_ms", "time", "timestamp")
                    side   = _get(d, "side", "S", "s")
                    qtyRaw = _get(d, "qty", "v", "size", "q", default=0.0)
                    try:
                        ts  = int(ts_raw)
                        qty = float(qtyRaw or 0.0)
                        if ts and side and qty > 0:
                            self.flow.add_trade(ts, str(side), qty)
                            self.cvd.on_trade(ts, str(side), qty)
                            added += 1
                    except Exception:
                        pass

                ofi_z   = float(self.flow.ofi_zscore())
                ofi_len = int(getattr(self.flow, "_dbg_ofi_len", 0))
                ofi_win = int(getattr(self.flow, "_dbg_ofi_win", 0))
                cvd_above_ema = self.cvd.slope_positive
                seq_buys = self.cvd.seq_mkt_buys
                seq_sells = self.cvd.seq_mkt_sells
                cvd_slope_val = float(self.cvd.slope_z()) if hasattr(self.cvd, "slope_z") else 0.0
                # 方向を問わない強フロー票
                votes      = int(self._vote_count())

                self._metrics.update({
                    "ofi_z": ofi_z,
                    "cons_buy": seq_buys,
                    "cons_sell": seq_sells,
                    "cvd_slope_z": cvd_slope_val,
                    "edge_votes": votes,
                    "dbg_trades_seen": seen,     # ←追加
                    "dbg_trades_added": added,   # ←追加
                })

                # liquidation cluster
                liq_ok = None
                if config.LIQ_USE and self._price:
                    liqs = self.ws.snapshot_liq()
                    liq_ok = has_liq_cluster(self._price, liqs)

                with self._lock:
                    # 既存の指標を落としたくないなら、一度コピーして update が安全
                    d = dict(self._metrics)
                    d.update({
                        "price": self._price,
                        "obi": obi,                       # ← 直前で算出している前提
                        "ofi_z": ofi_z,
                        "ofi_len": ofi_len,  
                        "ofi_win": ofi_win,
                        "cvd_above_ema": cvd_above_ema,

                        # 互換のため両方入れておく（呼び元が seq_* か cons_* どちらを見るか不明なため）
                        "seq_buys":  seq_buys,
                        "seq_sells": seq_sells,
                        "cons_buy":  seq_buys,
                        "cons_sell": seq_sells,

                        "cvd_slope_z": cvd_slope_val,
                        "edge_votes": votes,

                        # 清算クラスタとΔOIの判定もメトリクスへ明示的に載せる
                        "liq_cluster_ok": liq_ok,
                        "doi_up_ok": self._doi_up_ok,
                        # ...
                    })
                    self._metrics = d
            except Exception:
                pass
            time.sleep(1)

    # ---- public API ----
    def update_regime(self, df: pd.DataFrame):
        """df: あなたの5m/15m OHLCV DataFrame"""
        try:
            atrp = atr_percent(df, period=14)
            adxv = adx(df, period=14)
            self._regime_ok = (atrp >= config.ATR_PCT_THR) or (adxv >= config.ADX_THR)
        except Exception:
            self._regime_ok = False

    def is_active_hours_jst(self) -> bool:
        # JSTで16:00–02:00
        now = dt.datetime.utcnow() + dt.timedelta(hours=9)
        (sh, sm, ss), (eh, em, es) = self.jst_active_hours
        start = now.replace(hour=sh, minute=sm, second=ss, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=es, microsecond=0)
        if eh < sh:  # 跨ぎ
            if now >= start or now <= end:
                return True
        return start <= now <= end

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._metrics)

    def pick_signal(self) -> Optional[str]:
        metrics = self.get_metrics_snapshot()
        sig, reasons = decide_signal(metrics, self._regime_ok, self.is_active_hours_jst())
        self.last_reasons = reasons
        return sig
