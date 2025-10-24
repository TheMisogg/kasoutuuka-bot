import json, os, requests

WEBHOOK = os.getenv("SLACK_WEBHOOK_URL")

def notify_slack(msg: str):
    if not WEBHOOK:
        return
    try:
        requests.post(WEBHOOK, json={"text": msg}, timeout=3)
    except Exception:
        pass
