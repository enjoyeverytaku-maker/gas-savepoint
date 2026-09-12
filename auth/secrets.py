"""Secret Manager経由での秘密情報アクセス。
OAuth Client Secret・Refresh Tokenを平文保存しないための共通ヘルパー（spec.md §14）。
"""
import os
import threading

from google.api_core.exceptions import NotFound
from google.cloud import secretmanager

_PROJECT_ID = os.environ.get("GCP_PROJECT")

# プロセス共有のシングルトン（firestore_client.pyと同じ理由：呼び出しごとに新規Clientを
# 生成するとgRPCチャンネルが積み上がりクラッシュの原因になるため、2026-09-12修正）。
_client_instance: secretmanager.SecretManagerServiceClient | None = None
_client_lock = threading.Lock()


def _client() -> secretmanager.SecretManagerServiceClient:
    global _client_instance
    if _client_instance is not None:
        return _client_instance
    with _client_lock:
        if _client_instance is None:
            _client_instance = secretmanager.SecretManagerServiceClient()
        return _client_instance


def get_secret(secret_id: str, version: str = "latest") -> str:
    """Secret Managerから最新（または指定バージョン）の値を取得する。"""
    name = f"projects/{_PROJECT_ID}/secrets/{secret_id}/versions/{version}"
    response = _client().access_secret_version(name=name)
    return response.payload.data.decode("utf-8")


def set_secret(secret_id: str, value: str) -> None:
    """値を新しいバージョンとして追加する。シークレット自体が無ければ作成する。"""
    client = _client()
    parent = f"projects/{_PROJECT_ID}"
    secret_path = f"{parent}/secrets/{secret_id}"
    try:
        client.get_secret(name=secret_path)
    except NotFound:
        client.create_secret(
            parent=parent,
            secret_id=secret_id,
            secret={"replication": {"automatic": {}}},
        )
    client.add_secret_version(parent=secret_path, payload={"data": value.encode("utf-8")})
