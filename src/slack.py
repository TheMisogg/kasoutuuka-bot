
import os, json, urllib.request

def notify_slack(text: str) -> None:
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return
    try:
        data = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as _:
            pass
    except Exception as e:
        print(f"[Slack通知失敗] {e}")
# --- ここは「元の notify_slack 定義の直後」に貼り付け ---
# 元の送信関数を退避
_notify_slack_raw = notify_slack

# レート制限パラメタ（未設定なら既定値で動く）
_SLACK_MIN_INTERVAL = float(getattr(S, "slack_min_interval_sec", 1.2))  # 最短間隔
_SLACK_BURST        = int(getattr(S, "slack_burst", 4))                 # バースト許容量

# トークンバケットと送信キュー
_SLACK_BUCKET = {"tokens": float(_SLACK_BURST), "last": time.monotonic()}
_SLACK_QUEUE  = []

def _slack_refill():
    now = time.monotonic()
    elapsed = now - _SLACK_BUCKET["last"]
    # 経過時間に応じてトークン補充（capacityは_BURST）
    _SLACK_BUCKET["tokens"] = min(
        float(_SLACK_BURST),
        float(_SLACK_BUCKET["tokens"]) + elapsed / _SLACK_MIN_INTERVAL
    )
    _SLACK_BUCKET["last"] = now

def notify_slack(message: str):
    """レート制限付きSlack送信。余剰はキューへ積む"""
    if not message:
        return
    _slack_refill()
    if _SLACK_BUCKET["tokens"] >= 1.0:
        _SLACK_BUCKET["tokens"] -= 1.0
        try:
            return _notify_slack_raw(message)
        except Exception as e:
            # 429等はバーストを空にして背圧
            if "429" in str(e) or "Too Many Requests" in str(e):
                _SLACK_BUCKET["tokens"] = 0.0
                # 失敗分はキューに戻しておく
                _SLACK_QUEUE.append(message)
            # それ以外の例外は握りつぶさずログ側で見えるように再送しない
            return
    else:
        _SLACK_QUEUE.append(message)

def _flush_slack_queue():
    """キューから送れるだけ送る（毎ループ呼び出し）"""
    _slack_refill()
    sent = 0
    while _SLACK_QUEUE and _SLACK_BUCKET["tokens"] >= 1.0:
        _SLACK_BUCKET["tokens"] -= 1.0
        msg = _SLACK_QUEUE.pop(0)
        try:
            _notify_slack_raw(msg)
        except Exception as e:
            if "429" in str(e) or "Too Many Requests" in str(e):
                _SLACK_BUCKET["tokens"] = 0.0
                _SLACK_QUEUE.insert(0, msg)  # 先頭に戻して次回へ
                break
            # 他エラーは破棄
            break
