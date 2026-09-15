"""プロジェクト単位メンバーシップ（パートナーズ版members_admin.py相当）。

GASプロジェクトごとにowner/maintainer/developer/viewerを個別に割り当てる。
グローバルadminは常に全プロジェクトへowner相当でアクセスできるが、それ以外のユーザーは
ここで個別に付与されない限りアクセス権を持たない（グローバルロールへのフォールバックは
行わない。auth/users.py get_project_role参照。2026-09-12の権限再設計でフォールバックを
廃止したのにこの説明文が古いままだったため2026-09-15に修正）。

メールアドレスはauth/users.normalize_emailで正規化してからドキュメントIDに使う
（大文字小文字の違いで権限が引けなくなるのを防ぐ、2026-09-15）。
"""
from __future__ import annotations

from typing import Any

from firestore_client import db
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from auth.users import VALID_PROJECT_ROLES, normalize_email

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
    return sorted(result, key=lambda member: member.get("email", ""))


def upsert_member(project_id: str, email: str, role: str, updated_by: str) -> dict[str, Any]:
    """プロジェクトへメンバーを追加・ロール更新する。"""
    if role not in VALID_PROJECT_ROLES:
        raise ValueError(f"invalid role: {role}")
    email = normalize_email(email)
    if not email:
        raise ValueError("email is required")
    doc_ref = _members_ref(project_id).document(email)
    doc_ref.set(
        {"role": role, "updated_by": updated_by, "updated_at": SERVER_TIMESTAMP},
        merge=True,
    )
    snapshot = doc_ref.get()
    data = snapshot.to_dict() or {}
    data["email"] = email
    data["updated_at"] = _to_iso(data.get("updated_at"))
    return data


def set_members_bulk(email: str, assignments: dict[str, str | None], updated_by: str) -> dict[str, int]:
    """1人の利用者について、複数GASの権限をまとめて設定する（2026-09-15、会長要望）。

    assignments: {project_id: ロール}。ロールにNoneを渡すとそのGASからは外す。
    GASを1件ずつ開いて1人ずつ追加するのは、GAS数×人数の操作が必要で現実的でないため、
    「人を起点にまとめて設定」「複数のGASへまとめて追加」の両方からこの関数を使う。

    Firestoreのバッチで一括適用し、途中で失敗して中途半端な権限状態になるのを防ぐ。
    """
    email = normalize_email(email)
    if not email:
        raise ValueError("email is required")
    for role in assignments.values():
        if role is not None and role not in VALID_PROJECT_ROLES:
            raise ValueError(f"invalid role: {role}")

    client = db()
    batch = client.batch()
    added = removed = 0
    for project_id, role in assignments.items():
        ref = (
            client.collection(PROJECTS_COLLECTION)
            .document(project_id)
            .collection(MEMBERS_SUBCOLLECTION)
            .document(email)
        )
        if role is None:
            batch.delete(ref)
            removed += 1
        else:
            batch.set(
                ref,
                {"role": role, "updated_by": updated_by, "updated_at": SERVER_TIMESTAMP},
                merge=True,
            )
            added += 1
    batch.commit()
    return {"assigned": added, "removed": removed}


def list_projects_for_member(email: str) -> dict[str, str]:
    """指定利用者が個別付与されているGASとそのロールを返す（{project_id: role}）。"""
    email = normalize_email(email)
    if not email:
        return {}
    result: dict[str, str] = {}
    for snapshot in db().collection_group(MEMBERS_SUBCOLLECTION).stream():
        if snapshot.id != email:
            continue
        project_ref = snapshot.reference.parent.parent
        if project_ref is not None:
            result[project_ref.id] = (snapshot.to_dict() or {}).get("role", "viewer")
    return result


def remove_member(project_id: str, email: str) -> None:
    """プロジェクトからメンバーを削除する（削除後はそのプロジェクトへのアクセス権を失う。
    グローバルadminだけは引き続きowner相当でアクセスできる）。"""
    _members_ref(project_id).document(normalize_email(email)).delete()


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
