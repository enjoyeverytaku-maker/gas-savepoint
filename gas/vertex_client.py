"""Vertex AI経由のGemini共有クライアント。

このアプリは既にGCP（gas-savepointプロジェクト）上で稼働しており、Cloud Run/
ローカルのApplication Default Credentials（ADC）でVertex AIを呼び出せるため、
Google AI Studio発行の別建てAPIキーとSecret Managerでの管理は不要
（会長指摘、2026-09-13。従来はsavepoint-gemini-api-keyシークレットを使用していた）。

Firestoreクライアント（firestore_client.py）と同じ理由でプロセス内シングルトンに
キャッシュする。
"""
from __future__ import annotations

import os
import threading

from google import genai

GEMINI_MODEL = "gemini-2.5-flash"
VERTEX_LOCATION = "us-central1"

_client: genai.Client | None = None
_lock = threading.Lock()


def get_genai_client() -> genai.Client:
    """共有Geminiクライアント（Vertex AI・ADC認証）を返す。"""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _client = genai.Client(
                vertexai=True,
                project=os.environ.get("GCP_PROJECT"),
                location=VERTEX_LOCATION,
            )
        return _client
