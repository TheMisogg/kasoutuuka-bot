# src/env.py
from __future__ import annotations
import os
from dotenv import load_dotenv, find_dotenv

def load_env() -> None:
    """
    現在の作業ディレクトリから親方向に .env を探索して読み込む。
    既に環境変数に入っている値は上書きしない。
    """
    path = find_dotenv(usecwd=True)  # ルート直下の .env が見つかるはず
    if path:
        load_dotenv(path, override=False)

# import だけでも最低限ロードしておく（明示呼び出し推奨）
try:
    load_env()
except Exception:
    pass

# 便利変数（必要に応じて使う）
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL") or os.getenv("SLACK_WEBHOOK")
BYBIT_API_KEY     = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET  = os.getenv("BYBIT_API_SECRET")
