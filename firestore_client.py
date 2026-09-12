"""Firestoreクライアントのプロセス内共有インスタンス。

各モジュールが個別にfirestore.Client()を生成していたところ、ダッシュボードの
複数プロジェクト同時読み込み等で短時間に大量の新規gRPCチャネルが生成され、
macOSのgRPC実装がfork安全性チェックで不安定になる問題が確認された
（auth/oauth.pyのOAuth認証情報キャッシュと同種の問題）。Firestoreクライアントは
スレッドセーフに設計されているため、プロセス内で1個だけ生成して使い回す。
"""
from __future__ import annotations

import os
import threading

from google.cloud import firestore

_client: firestore.Client | None = None
_lock = threading.Lock()


def db() -> firestore.Client:
    """共有Firestoreクライアントを返す（初回呼び出し時のみ生成）。"""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _client = firestore.Client(project=os.environ.get("GCP_PROJECT"))
        return _client
