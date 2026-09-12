"""操作履歴（監査ログ）の記録・読み取り（F7、spec.md §15）。"""
from __future__ import annotations

import os
from typing import Any

from google.cloud import firestore
from firestore_client import db

COLLECTION = "operation_logs"

# spec.md §15のAction例
ACTION_LOGIN = "LOGIN"
ACTION_GAS_PROJECT_CREATE = "GAS_PROJECT_CREATE"
ACTION_GAS_PROJECT_UPDATE = "GAS_PROJECT_UPDATE"
ACTION_SOURCE_FETCH = "SOURCE_FETCH"
ACTION_ROLLBACK = "ROLLBACK"
ACTION_VERSION_CREATE = "VERSION_CREATE"
ACTION_RELEASE_REQUEST = "RELEASE_REQUEST"
ACTION_CHANGE_DETECT = "CHANGE_DETECT"
ACTION_CHANGE_REVIEW = "CHANGE_REVIEW"
ACTION_CHANGE_APPROVE = "CHANGE_APPROVE"


def log_operation(
    action: str,
    user: str,
    project_id: str = "",
    target_version: int | None = None,
    result: str = "success",
    details: dict[str, Any] | None = None,
) -> None:
    """操作履歴を1件記録する。project_idはプロジェクトに紐付かない操作（LOGIN等）では省略可。"""
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


def list_operations(limit: int = 200) -> list[dict[str, Any]]:
    """操作履歴を新しい順で取得する（Admin向け、F7）。"""
    query = db().collection(COLLECTION).order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
    return [serialize_log(snapshot) for snapshot in query.stream()]


def serialize_log(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    """Firestoreの操作履歴文書をAPI用に整形する。"""
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data["timestamp"] = _to_iso(data.get("timestamp"))
    return data


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
