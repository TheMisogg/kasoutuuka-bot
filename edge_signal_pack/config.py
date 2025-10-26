import os

# === 基本 ===
SYMBOL = os.getenv("SYMBOL", "SOLUSDT")
TIMEFRAME_BASE = os.getenv("TIMEFRAME_BASE", "5m")   # "5m" or "15m"

# === レジーム判定 ===
ATR_PCT_THR = float(os.getenv("ATR_PCT_THR", "0.006"))  # 0.8% = 0.008
ADX_THR      = float(os.getenv("ADX_THR", "16"))


# === OBI（板不均衡） ===
OBI_LEVELS = int(os.getenv("OBI_LEVELS", "8"))
OBI_THR    = float(os.getenv("OBI_THR", "0.5"))

# === OFI（約定フロー不均衡） ===
OFI_WINDOW_SEC = int(os.getenv("OFI_WINDOW_SEC", "30"))
OFI_Z_THR      = float(os.getenv("OFI_Z_THR", "2.0"))

# === CVD（累積Delta） ===
CVD_EMA = int(os.getenv("CVD_EMA", "20"))
SEQ_MKT_TICKS = int(os.getenv("SEQ_MKT_TICKS", "20"))  # 1秒足の連続成行

# === 清算クラスター ===
LIQ_USE = os.getenv("LIQ_USE", "1") == "1"
LIQ_CLUSTER_PCT = float(os.getenv("LIQ_CLUSTER_PCT", "0.003"))  # 0.3%
LIQ_CLUSTER_USD = float(os.getenv("LIQ_CLUSTER_USD", "200000"))

# === ΔOI ===
DOI_USE = os.getenv("DOI_USE", "1") == "1"
DOI_5M_PCT = float(os.getenv("DOI_5M_PCT", "0.005"))

# === Slack ===
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

# 強フローで regime gate をバイパスする閾値（方向問わず）
REGIME_OVERRIDE_OFI_Z = 2.2
REGIME_OVERRIDE_CONS  = 3   # seq_buys/sells のどちらかが3以上
