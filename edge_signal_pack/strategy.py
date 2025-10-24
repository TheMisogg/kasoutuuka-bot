from typing import Optional, Dict, Any, List, Tuple
from . import config

def has_liq_cluster(spot_price: float, liqs: list) -> bool:
    # 価格±LIQ_CLUSTER_PCT 内に合計USDが一定以上の清算があるか
    if not liqs:
        return False
    band = config.LIQ_CLUSTER_PCT * spot_price
    lo, hi = spot_price - band, spot_price + band
    total = 0.0
    for d in liqs[-200:]:  # 直近だけ見る
        p = d.get("price", 0.0)
        q = d.get("qty", 0.0)
        if p >= lo and p <= hi:
            total += p * q
    return total >= config.LIQ_CLUSTER_USD

def decide_signal(metrics: Dict[str, Any], regime_ok: bool, jst_active_ok: bool) -> Tuple[Optional[str], List[str]]:
    """
    metrics keys:
      - price, obi, ofi_z, cvd_above_ema (bool)
      - seq_buys, seq_sells (int)
      - liq_cluster_ok, doi_up_ok (bool or None)
    """
    reasons: List[str] = []
    # 1) 時間外などの致命ガードは最優先
    if not jst_active_ok:
        return None, ["inactive hours"]

    # 2) 強フロー例外（regime not ok でも先に通す）
    ofi_z = float(metrics.get("ofi_z", 0.0))
    seq_buys = int(metrics.get("seq_buys", 0))
    seq_sells = int(metrics.get("seq_sells", 0))
    # しきい値は無ければクールダウン用/既定値をフォールバック
    th_ofi = float(getattr(config, "REGIME_OVERRIDE_OFI_Z",
                    getattr(config, "COOLDOWN_OVERRIDE_OFI_Z", 2.2)))
    th_cons = int(getattr(config, "REGIME_OVERRIDE_CONS",
                    getattr(config, "COOLDOWN_OVERRIDE_CONS", 3)))
    strong_flow = (abs(ofi_z) >= th_ofi) or (max(seq_buys, seq_sells) >= th_cons)

    if not regime_ok and not strong_flow:
        return None, ["regime not ok"]
    if not regime_ok and strong_flow:
        reasons.append("regime override by strong_flow")
    # LONG条件
    long_votes = 0
    if metrics.get("obi", 0) >= config.OBI_THR:
        long_votes += 1; reasons.append("OBI↑")
    if metrics.get("ofi_z", 0) >= config.OFI_Z_THR:
        long_votes += 1; reasons.append("OFI z↑")
    if metrics.get("cvd_above_ema", False) and metrics.get("seq_buys", 0) >= config.SEQ_MKT_TICKS:
        long_votes += 1; reasons.append("CVD↑ & 連続買い")
    liq_ok = metrics.get("liq_cluster_ok", None)
    if liq_ok is True:
        long_votes += 1; reasons.append("清算クラスター↑")
    doi_ok = metrics.get("doi_up_ok", None)
    if doi_ok is True:
        long_votes += 1; reasons.append("ΔOI上昇")

    if long_votes >= 2:
        reasons.insert(0, f"LONG votes={long_votes}")
        return "LONG", reasons

    # SHORT条件
    reasons.clear()
    short_votes = 0
    if metrics.get("obi", 0) <= -config.OBI_THR:
        short_votes += 1; reasons.append("OBI↓")
    if metrics.get("ofi_z", 0) <= -config.OFI_Z_THR:
        short_votes += 1; reasons.append("OFI z↓")
    if (not metrics.get("cvd_above_ema", True)) and metrics.get("seq_sells", 0) >= config.SEQ_MKT_TICKS:
        short_votes += 1; reasons.append("CVD↓ & 連続売り")
    if liq_ok is True:
        short_votes += 1; reasons.append("清算クラスター↓")
    if doi_ok is True:
        short_votes += 1; reasons.append("ΔOI上昇→下落圧?")

    if short_votes >= 2:
        reasons.insert(0, f"SHORT votes={short_votes}")
        return "SHORT", reasons

    return None, ["no consensus"]
