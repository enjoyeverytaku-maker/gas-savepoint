"""GASプロジェクトCRUD。"""
from __future__ import annotations

import os
from typing import Any

from google.cloud import firestore
from firestore_client import db
from google.cloud.firestore_v1 import SERVER_TIMESTAMP


COLLECTION = "gas_projects"
DEFAULT_GOOGLE_ACCOUNT = "connected_google_account"


def create_project(data: dict[str, Any]) -> dict[str, Any]:
    """GASプロジェクトをFirestoreへ登録する。"""
    now_fields = {
        "created_at": SERVER_TIMESTAMP,
        "updated_at": SERVER_TIMESTAMP,
    }
    payload = {
        "project_name": data["project_name"],
        "script_id": data["script_id"],
        "google_account": data.get("google_account") or DEFAULT_GOOGLE_ACCOUNT,
        "description": data.get("description", ""),
        "status": data.get("status", ""),
        "department": data.get("department", ""),
        **now_fields,
    }
    _, doc_ref = db().collection(COLLECTION).add(payload)
    snapshot = doc_ref.get()
    return serialize_project(snapshot)


def list_projects() -> list[dict[str, Any]]:
    """GASプロジェクト一覧を取得する。"""
    query = db().collection(COLLECTION).order_by("created_at", direction=firestore.Query.DESCENDING)
    return [serialize_project(snapshot) for snapshot in query.stream()]


def get_project(project_id: str) -> dict[str, Any] | None:
    """GASプロジェクト詳細を取得する。"""
    snapshot = db().collection(COLLECTION).document(project_id).get()
    if not snapshot.exists:
        return None
    return serialize_project(snapshot)


EDITABLE_FIELDS = ("project_name", "google_account", "description", "status", "department")


def update_project(project_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """GASプロジェクトの台帳項目を更新する（F8）。script_idは登録後は変更しない（別プロジェクトとして再登録する）。"""
    doc_ref = db().collection(COLLECTION).document(project_id)
    if not doc_ref.get().exists:
        return None
    payload = {key: value for key, value in updates.items() if key in EDITABLE_FIELDS and value is not None}
    payload["updated_at"] = SERVER_TIMESTAMP
    doc_ref.update(payload)
    return serialize_project(doc_ref.get())


def set_readme(project_id: str, readme_markdown: str) -> None:
    """AI生成READMEをプロジェクト文書へ保存する。"""
    db().collection(COLLECTION).document(project_id).update(
        {
            "readme_markdown": readme_markdown,
            "readme_generated_at": SERVER_TIMESTAMP,
        }
    )


def serialize_project(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    """Firestoreのプロジェクト文書をAPI用に整形する。"""
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data["created_at"] = _to_iso(data.get("created_at"))
    data["updated_at"] = _to_iso(data.get("updated_at"))
    return data


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
