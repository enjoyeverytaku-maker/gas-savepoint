"""リリース申請・承認フロー（F5）。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from auth.oauth import get_credentials

from .apps_script import fetch_source_files
from .audit import (
    ACTION_RELEASE_APPROVE,
    ACTION_RELEASE_REJECT,
    ACTION_RELEASE_REQUEST,
    log_operation,
)
from .diff import calculate_diff, calculate_source_hash
from .projects import get_project
from .savepoints import create_savepoint, get_latest_savepoint


COLLECTION = "releases"


@dataclass
class ReleaseNotFoundError(Exception):
    """リリースまたはプロジェクトが存在しない。"""

    message: str


@dataclass
class ReleaseNotPendingError(Exception):
    """リリースが承認待ちではない。"""

    message: str
    current_status: str


@dataclass
class NoChangesToReleaseError(Exception):
    """リリース申請できる変更が存在しない。"""

    message: str


def db() -> firestore.Client:
    """Firestoreクライアントを生成する。"""
    return firestore.Client(project=os.environ.get("GCP_PROJECT"))


def create_release(project_id: str, requested_by: str, comment: str = "") -> dict[str, Any]:
    """現在のGASソースをリリース申請として保存する。"""
    project = get_project(project_id)
    if project is None:
        raise ReleaseNotFoundError(f"GASプロジェクトが見つかりません: {project_id}")

    source_files = fetch_source_files(project["script_id"], get_credentials())
    latest_savepoint = get_latest_savepoint(project_id)
    previous_files = latest_savepoint["source_files"] if latest_savepoint else []
    diff = calculate_diff(source_files, previous_files)
    if not diff:
        raise NoChangesToReleaseError(f"リリース申請できる変更がありません: {project_id}")

    payload = {
        "project_id": project_id,
        "status": "pending",
        "comment": comment,
        "source_files": source_files,
        "source_hash": calculate_source_hash(source_files),
        "diff": diff,
        "requested_by": requested_by,
        "requested_at": SERVER_TIMESTAMP,
        "approved_by": None,
        "approved_at": None,
        "rejected_by": None,
        "rejected_at": None,
        "rejected_reason": None,
        "resulting_version_no": None,
    }
    _, doc_ref = db().collection(COLLECTION).add(payload)
    log_operation(
        action=ACTION_RELEASE_REQUEST,
        project_id=project_id,
        user=requested_by,
        result="success",
    )
    return serialize_release(doc_ref.get())


def list_releases(project_id: str) -> list[dict[str, Any]]:
    """指定プロジェクトのリリース申請一覧を新しい順で取得する。

    where(project_id)とorder_by(requested_at)を同時に使うとFirestoreの複合インデックスが
    必須になる（未作成だとFailedPrecondition）ため、savepoints.list_savepointsと同様に
    Python側でソートする。
    """
    query = db().collection(COLLECTION).where("project_id", "==", project_id)
    releases = [serialize_release(snapshot) for snapshot in query.stream()]
    return sorted(releases, key=lambda item: item.get("requested_at") or "", reverse=True)


def get_release(release_id: str) -> dict[str, Any] | None:
    """リリース申請を1件取得する。"""
    snapshot = db().collection(COLLECTION).document(release_id).get()
    if not snapshot.exists:
        return None
    return serialize_release(snapshot)


def approve_release(release_id: str, approved_by: str) -> dict[str, Any]:
    """承認待ちリリースを正式なセーブポイントへ昇格する。"""
    release = get_release(release_id)
    if release is None:
        raise ReleaseNotFoundError(f"リリース申請が見つかりません: {release_id}")
    if release.get("status") != "pending":
        current_status = str(release.get("status", ""))
        raise ReleaseNotPendingError(
            message=f"リリース申請は承認待ちではありません: {release_id}",
            current_status=current_status,
        )

    savepoint = create_savepoint(
        project_id=release["project_id"],
        source_files=release["source_files"],
        comment=f"リリース承認: {release.get('comment') or '(コメントなし)'}",
        created_by=approved_by,
    )
    resulting_version_no = int(savepoint["version_no"])
    db().collection(COLLECTION).document(release_id).update(
        {
            "status": "approved",
            "approved_by": approved_by,
            "approved_at": SERVER_TIMESTAMP,
            "resulting_version_no": resulting_version_no,
        }
    )
    log_operation(
        action=ACTION_RELEASE_APPROVE,
        project_id=release["project_id"],
        user=approved_by,
        target_version=resulting_version_no,
        result="success",
    )
    updated = get_release(release_id)
    if updated is None:
        raise ReleaseNotFoundError(f"リリース申請が見つかりません: {release_id}")
    return {"release": updated, "savepoint": savepoint}


def reject_release(release_id: str, rejected_by: str, reason: str = "") -> dict[str, Any]:
    """承認待ちリリースを却下する。"""
    release = get_release(release_id)
    if release is None:
        raise ReleaseNotFoundError(f"リリース申請が見つかりません: {release_id}")
    if release.get("status") != "pending":
        current_status = str(release.get("status", ""))
        raise ReleaseNotPendingError(
            message=f"リリース申請は承認待ちではありません: {release_id}",
            current_status=current_status,
        )

    db().collection(COLLECTION).document(release_id).update(
        {
            "status": "rejected",
            "rejected_by": rejected_by,
            "rejected_at": SERVER_TIMESTAMP,
            "rejected_reason": reason,
        }
    )
    log_operation(
        action=ACTION_RELEASE_REJECT,
        project_id=release["project_id"],
        user=rejected_by,
        result="success",
        details={"reason": reason},
    )
    updated = get_release(release_id)
    if updated is None:
        raise ReleaseNotFoundError(f"リリース申請が見つかりません: {release_id}")
    return updated


def serialize_release(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    """Firestoreのリリース文書をAPI用に整形する。"""
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data["requested_at"] = _to_iso(data.get("requested_at"))
    data["approved_at"] = _to_iso(data.get("approved_at"))
    data["rejected_at"] = _to_iso(data.get("rejected_at"))
    return data


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
