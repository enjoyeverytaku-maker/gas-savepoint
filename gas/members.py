"""プロジェクト単位メンバーシップ（パートナーズ版members_admin.py相当）。

GASプロジェクトごとにowner/maintainer/developer/viewerを個別に割り当てる。
未設定のユーザーはauth/users.pyのグローバルロールにフォールバックする
（auth/users.py get_project_role参照）。
"""
from __future__ import annotations

import os
from typing import Any

from google.cloud import firestore
from firestore_client import db
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from auth.users import VALID_ROLES

PROJECTS_COLLECTION = "gas_projects"
MEMBERS_SUBCOLLECTION = "members"


def _members_ref(project_id: str):
    return db().collection(PROJECTS_COLLECTION).document(project_id).collection(MEMBERS_SUBCOLLECTION)


def list_members(project_id: str) -> list[dict[str, Any]]:
    """指定プロジェクトのメンバー一覧を返す。"""
    result = []
    for snapshot in _members_ref(project_id).stream():
        data = snapshot.to_dict() or {}
        data["email"] = snapshot.id
        data["updated_at"] = _to_iso(data.get("updated_at"))
        result.append(data)
    return result


def upsert_member(project_id: str, email: str, role: str, updated_by: str) -> dict[str, Any]:
    """プロジェクトへメンバーを追加・ロール更新する。"""
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role}")
    _members_ref(project_id).document(email).set(
        {"role": role, "updated_by": updated_by, "updated_at": SERVER_TIMESTAMP},
        merge=True,
    )
    snapshot = _members_ref(project_id).document(email).get()
    data = snapshot.to_dict() or {}
    data["email"] = email
    data["updated_at"] = _to_iso(data.get("updated_at"))
    return data


def remove_member(project_id: str, email: str) -> None:
    """プロジェクトからメンバーを削除する（削除後はグローバルロールへフォールバックする）。"""
    _members_ref(project_id).document(email).delete()


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
