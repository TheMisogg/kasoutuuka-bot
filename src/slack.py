# src/slack.py
import os, time, json, threading
from collections import deque
from urllib import request, error

# ========= 設定（必要ならSTRATEGYや.envで上書き） =========
_MIN_INTERVAL_SEC     = float(os.getenv("SLACK_MIN_INTERVAL_SEC", "1.5"))  # 1.5秒/件
_BURST_TOKENS         = float(os.getenv("SLACK_BURST", "3"))               # 初期バースト
_DRAIN_PER_FLUSH      = int(os.getenv("SLACK_DRAIN_PER_TICK", "2"))        # 1回のflushで送る最大件数
_DEFAULT_RETRY_SEC    = float(os.getenv("SLACK_RETRY_DEFAULT_SEC", "60"))  # Retry-Afterが無い429用
# ============================================================

_WEBHOOK_URL   = os.getenv("SLACK_WEBHOOK_URL")
_BOT_TOKEN     = os.getenv("SLACK_BOT_TOKEN")        # xoxb-...
_CHANNEL_ID    = os.getenv("SLACK_CHANNEL_ID")       # Cxxxx のID（#general などの名前ではなくID）

_SLACK_QUEUE   = deque()      # (text, payload_dict)
_LAST_SEND_AT  = 0.0
_LAST_TOKENS_AT= 0.0  
_TOKENS        = _BURST_TOKENS
_SUSPEND_UNTIL = 0.0
_LOCK          = threading.Lock()

def slack_configured() -> bool:
    return bool((_BOT_TOKEN and _CHANNEL_ID) or _WEBHOOK_URL)

def notify_slack(text: str, **kwargs) -> None:
    """
    送信要求をキューに積む。kwargsは blocks 等の追加フィールド用。
    """
    with _LOCK:
        _SLACK_QUEUE.append((text, kwargs))

def _refill_tokens():
    """時間経過で送信トークンを回復。_LAST_SEND_AT は“送信”時のみ更新する。"""
    global _TOKENS, _LAST_TOKENS_AT
    now = time.monotonic()
    if _LAST_TOKENS_AT == 0.0:
        _LAST_TOKENS_AT = now
        return
    rate = 1.0 / max(_MIN_INTERVAL_SEC, 0.1)  # 1件 / _MIN_INTERVAL_SEC 秒
    _TOKENS = min(_BURST_TOKENS, _TOKENS + (now - _LAST_TOKENS_AT) * rate)
    _LAST_TOKENS_AT = now

def _suspend(sec: float):
    global _SUSPEND_UNTIL
    _SUSPEND_UNTIL = max(_SUSPEND_UNTIL, time.monotonic() + max(sec, 1.0))
    print(f"[Slack] Suspend {int(sec)}s (queued)")

def _send_via_webhook(text: str, payload_extra: dict):
    if not _WEBHOOK_URL:
        return
    body = {"text": text}
    body.update(payload_extra or {})
    data = json.dumps(body).encode("utf-8")
    req = request.Request(_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=8) as resp:
            # 2xx想定
            return
    except error.HTTPError as e:
        if e.code == 429:
            # 429: Retry-After が無いケースがある → 既定値でバックオフ
            ra = e.headers.get("Retry-After") or e.headers.get("retry-after")
            backoff = float(ra) if ra else _DEFAULT_RETRY_SEC
            _suspend(backoff)
            return
        # その他エラーは標準出力に一度だけ
        msg = e.read().decode("utf-8", "ignore")
        print(f"[Slack] webhook error {e.code}: {msg[:200]}")
        return

def _send_via_webapi(text: str, payload_extra: dict):
    if not (_BOT_TOKEN and _CHANNEL_ID):
        return
    url = "https://slack.com/api/chat.postMessage"
    body = {"channel": _CHANNEL_ID, "text": text}
    # blocks等は任意
    body.update(payload_extra or {})
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }
    req = request.Request(url, data=data, headers=headers)
    try:
        with request.urlopen(req, timeout=8) as resp:
            res = json.loads(resp.read().decode("utf-8", "ignore"))
            if not res.get("ok", False):
                err = res.get("error", "")
                if err in ("ratelimited", "message_limit_exceeded", "rate_limited"):
                    ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                    backoff = float(ra) if ra else _DEFAULT_RETRY_SEC
                    _suspend(backoff)
                else:
                    print(f"[Slack] api error: {err}")
            return
    except error.HTTPError as e:
        if e.code == 429:
            ra = e.headers.get("Retry-After") or e.headers.get("retry-after")
            backoff = float(ra) if ra else _DEFAULT_RETRY_SEC
            _suspend(backoff)
            return
        msg = e.read().decode("utf-8", "ignore")
        print(f"[Slack] api http error {e.code}: {msg[:200]}")
        return

def _send_one(text: str, payload_extra: dict):
    global _TOKENS, _LAST_SEND_AT
    # 優先：Bot Token、無ければWebhook
    if _BOT_TOKEN and _CHANNEL_ID:
        _send_via_webapi(text, payload_extra)
    else:
        _send_via_webhook(text, payload_extra)
    _TOKENS -= 1.0
    _LAST_SEND_AT = time.monotonic()

def _can_send_now() -> bool:
    if time.monotonic() < _SUSPEND_UNTIL:
        return False
    if _TOKENS < 1.0:
        return False
    # 最低間隔
    if (time.monotonic() - _LAST_SEND_AT) < _MIN_INTERVAL_SEC:
        return False
    return True

def _flush_once():
    global _LAST_SEND_AT
    if not slack_configured():
        return
    _refill_tokens()
    sent = 0
    while _SLACK_QUEUE and _can_send_now() and sent < _DRAIN_PER_FLUSH:
        text, extra = _SLACK_QUEUE.popleft()
        _send_one(text, extra or {})
        sent += 1

def _flush_slack_queue():
    # 外部公開：main やワンライナーから呼ぶ
    _flush_once()

# 互換のため旧名も残す
flush = _flush_slack_queue
