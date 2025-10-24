import time, hmac, hashlib, requests, os, json

BASE = "https://api.bybit.com"

API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")

def market_open_interest(symbol="SOLUSDT", interval="5min"):
    """
    v5 open interest (public)
    GET /v5/market/open-interest
    """
    url = BASE + "/v5/market/open-interest"
    params = {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": interval,
    }
    try:
        r = requests.get(url, params=params, timeout=3)
        j = r.json()
        if j.get("retCode") != 0: 
            return None
        return j.get("result", {}).get("list", [])
    except Exception:
        return None

def funding_rate(symbol="SOLUSDT"):
    url = BASE + "/v5/market/funding/history"
    params = {"category": "linear", "symbol": symbol, "limit": 1}
    try:
        r = requests.get(url, params=params, timeout=3)
        j = r.json()
        if j.get("retCode") != 0:
            return None
        lst = j.get("result", {}).get("list", [])
        return lst[0] if lst else None
    except Exception:
        return None
