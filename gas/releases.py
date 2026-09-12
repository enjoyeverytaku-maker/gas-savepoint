"""リリース作成（F5、パートナーズ版releases_admin相当）。

パートナーズ版の実コードを確認した結果、「リリース」自体に承認/却下という
別工程は存在しない。承認が必要なのは個々の変更（gas/changes.py）であり、
リリースはプロジェクトの未リリースchangesが全件承認済みであることを条件に
まとめて記録する一括操作（作成した時点で確定）。作成直前に現在のGASを
再取得し、その場で新たな変更が検知されればリリースをブロックする
（会長確認済み: これは「承認するまで本番反映を止める」機能ではなく、
GASは保存時点で即座に反映されるため、事後の正式記録としての統制）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from google.cloud import firestore
from firestore_client import db
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from auth.oauth import get_credentials

from .apps_script import fetch_source_files
from .audit import ACTION_RELEASE_REQUEST, log_operation
from .changes import detect_change, get_unreleased_changes, mark_changes_released
from .projects import get_project
from .savepoints import create_savepoint


COLLECTION = "releases"


@dataclass
class ReleaseNotFoundError(Exception):
    """リリースまたはプロジェクトが存在しない。"""

    message: str


@dataclass
class NoChangesToReleaseError(Exception):
    """リリース対象の未リリース変更が存在しない。"""

    message: str


@dataclass
class ChangesNotApprovedError(Exception):
    """未承認の変更が残っているためリリースできない。"""

    message: str
    blocked_count: int


def create_release(project_id: str, requested_by: str, comment: str = "") -> dict[str, Any]:
    """未リリースの変更（全件承認済み）をまとめて正式なリリースとして記録する。"""
    project = get_project(project_id)
    if project is None:
        raise ReleaseNotFoundError(f"GASプロジェクトが見つかりません: {project_id}")

    # リリース直前に現在のGASを再取得し、レビューされていない駆け込み変更があれば検知する
    # （パートナーズ版のsync_script呼び出しと同じ意図）。
    source_files = fetch_source_files(project["script_id"], get_credentials())
    detect_change(project_id, source_files)

    changes = get_unreleased_changes(project_id)
    if not changes:
        raise NoChangesToReleaseError(f"リリース対象の未リリース変更がありません: {project_id}")

    blocked = [c for c in changes if c.get("approval_status") != "approved"]
    if blocked:
        raise ChangesNotApprovedError(
            message=f"未承認の変更が{len(blocked)}件残っています。すべて承認してからリリースしてください。",
            blocked_count=len(blocked),
        )

    savepoint = create_savepoint(
        project_id=project_id,
        source_files=source_files,
        comment=comment or "リリース",
        created_by=requested_by,
    )
    change_ids = [c["id"] for c in changes]
    doc_ref = db().collection(COLLECTION).document()
    release_id = doc_ref.id

    payload = {
        "project_id": project_id,
        "resulting_version_no": int(savepoint["version_no"]),
        "change_ids": change_ids,
        "change_count": len(change_ids),
        "comment": comment,
        "requested_by": requested_by,
        "created_at": SERVER_TIMESTAMP,
    }
    doc_ref.set(payload)
    mark_changes_released(change_ids, release_id=release_id)

    log_operation(
        action=ACTION_RELEASE_REQUEST,
        project_id=project_id,
        user=requested_by,
        target_version=int(savepoint["version_no"]),
        result="success",
        details={"change_count": len(change_ids)},
    )
    return {"release": serialize_release(doc_ref.get()), "savepoint": savepoint}


def list_releases(project_id: str) -> list[dict[str, Any]]:
    """指定プロジェクトのリリース履歴を新しい順で取得する。"""
    query = db().collection(COLLECTION).where("project_id", "==", project_id)
    releases = [serialize_release(snapshot) for snapshot in query.stream()]
    return sorted(releases, key=lambda item: item.get("created_at") or "", reverse=True)


def get_release(release_id: str) -> dict[str, Any] | None:
    """リリースを1件取得する。"""
    snapshot = db().collection(COLLECTION).document(release_id).get()
    if not snapshot.exists:
        return None
    return serialize_release(snapshot)


def serialize_release(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    """Firestoreのリリース文書をAPI用に整形する。"""
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data["created_at"] = _to_iso(data.get("created_at"))
    return data


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
