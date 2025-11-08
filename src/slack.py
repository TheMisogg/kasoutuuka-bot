import os, json, urllib.request, urllib.error, time

# --- STRATEGY を安全に取り込む（起動方法の違いに強い） ---
try:
    from .config import STRATEGY as S   # python -m src.main
except Exception:
    try:
        from config import STRATEGY as S  # python src/main.py
    except Exception:
        class _Empty: pass
        S = _Empty()

# ========== 生送信（429は上位でハンドルするのでここではprintしない） ==========
def _send_slack_raw(text: str) -> None:
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url or not text:
        return
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    # 成功すれば何も返さない。HTTPErrorは上位へ投げる。
    with urllib.request.urlopen(req, timeout=10) as _:
        return

# ========== レート制限 & リトライ（トークンバケット＋Retry-After） ==========
_SLACK_MIN_INTERVAL = float(getattr(S, "slack_min_interval_sec", 1.2))  # 最短間隔
_SLACK_BURST        = int(getattr(S, "slack_burst", 4))                 # バースト数
_SLACK_RETRY_DEFAULT= float(getattr(S, "slack_retry_default_sec", 30))  # Retry-Afterが無い場合の待機

_SLACK_BUCKET = {"tokens": float(_SLACK_BURST), "last": time.monotonic()}
_SLACK_QUEUE  = []             # 未送信キュー
_SLACK_SUSPEND_UNTIL = 0.0     # 429等で一時停止している間の終端時刻（monotonic秒）

def _retry_after_seconds(err: Exception) -> float:
    """HTTP 429 の Retry-After 秒数を取り出す（無ければ既定値）。"""
    if isinstance(err, urllib.error.HTTPError) and err.code == 429:
        try:
            ra = err.headers.get("Retry-After")
            if ra is None:  # 一部は小文字で来る
                ra = err.headers.get("retry-after")
            if ra:
                return float(ra)
        except Exception:
            pass
        return _SLACK_RETRY_DEFAULT
    return 0.0

def _slack_refill() -> None:
    """トークン補充。サスペンド中は補充しない（送信もさせない）。"""
    global _SLACK_BUCKET
    now = time.monotonic()
    if now < _SLACK_SUSPEND_UNTIL:
        return
    elapsed = now - _SLACK_BUCKET["last"]
    _SLACK_BUCKET["tokens"] = min(
        float(_SLACK_BURST),
        float(_SLACK_BUCKET["tokens"]) + elapsed / _SLACK_MIN_INTERVAL
    )
    _SLACK_BUCKET["last"] = now

def notify_slack(message: str) -> None:
    """外部公開：Slack送信（レート制限＆Retry-Afterつき）"""
    global _SLACK_SUSPEND_UNTIL, _SLACK_BUCKET
    if not message:
        return

    # サスペンド中はキューへ積むだけ
    if time.monotonic() < _SLACK_SUSPEND_UNTIL:
        _SLACK_QUEUE.append(message)
        return

    _slack_refill()
    if _SLACK_BUCKET["tokens"] >= 1.0:
        _SLACK_BUCKET["tokens"] -= 1.0
        try:
            _send_slack_raw(message)
        except Exception as e:
            # 429 → サスペンドし、メッセージをキューへ戻す
            wait = _retry_after_seconds(e)
            if wait > 0:
                _SLACK_BUCKET["tokens"] = 0.0
                _SLACK_SUSPEND_UNTIL = time.monotonic() + wait
                _SLACK_QUEUE.append(message)
                # コンソールだけに簡潔に通知（ループ氾濫を避ける）
                print(f"[Slack 429] Suspend {wait:.0f}s (queued)")
            else:
                # その他のエラーは一度だけ表示して破棄（無限リトライを避ける）
                print(f"[Slack通知失敗] {e}")
    else:
        _SLACK_QUEUE.append(message)

def _flush_slack_queue() -> None:
    """毎ループで呼ぶ。サスペンド解除後にキューから送信。"""
    global _SLACK_SUSPEND_UNTIL, _SLACK_BUCKET
    # サスペンド中は何もしない
    if time.monotonic() < _SLACK_SUSPEND_UNTIL:
        return

    _slack_refill()
    sent = 0
    while _SLACK_QUEUE and _SLACK_BUCKET["tokens"] >= 1.0:
        _SLACK_BUCKET["tokens"] -= 1.0
        msg = _SLACK_QUEUE.pop(0)
        try:
            _send_slack_raw(msg)
            sent += 1
        except Exception as e:
            wait = _retry_after_seconds(e)
            if wait > 0:
                _SLACK_BUCKET["tokens"] = 0.0
                _SLACK_SUSPEND_UNTIL = time.monotonic() + wait
                _SLACK_QUEUE.insert(0, msg)  # 先頭に戻して次回へ
                print(f"[Slack 429] Suspend {wait:.0f}s (queue back)")
                break
            else:
                # その他のエラーは破棄（無限リトライを避ける）
                print(f"[Slack通知失敗] {e}")
                break
