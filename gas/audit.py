"""操作履歴（監査ログ）の書き込み（F7、spec.md §15）。

読み取り・一覧表示APIはT10で別途実装する。ここではT6（ロールバック）が
自身の操作を記録するために先行して書き込み口だけを用意する。
"""
from __future__ import annotations

import os
from typing import Any

from google.cloud import firestore

COLLECTION = "operation_logs"

# spec.md §15のAction例
ACTION_ROLLBACK = "ROLLBACK"
ACTION_VERSION_CREATE = "VERSION_CREATE"


def db() -> firestore.Client:
    """Firestoreクライアントを生成する。"""
    return firestore.Client(project=os.environ.get("GCP_PROJECT"))


def log_operation(
    action: str,
    project_id: str,
    user: str,
    target_version: int | None = None,
    result: str = "success",
    details: dict[str, Any] | None = None,
) -> None:
    """操作履歴を1件記録する。"""
    db().collection(COLLECTION).add(
        {
            "timestamp": firestore.SERVER_TIMESTAMP,
            "user": user,
            "action": action,
            "project_id": project_id,
            "target_version": target_version,
            "result": result,
            "details": details or {},
        }
    )
