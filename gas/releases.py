"""セーブ（パートナーズ版releases_admin相当、社内呼称「セーブポイント作成」）。

以前は個々の変更（gas/changes.py）への人間のレビュー・承認が全件揃うことを
セーブの条件にしていたが、会長より「セーブポイントを作る際にそれまでの変更の
影響についてAIレビューしてくれるように」との指示を受け、人間の承認ゲートは
廃止した。代わりに、前回セーブポイントからの累積差分をAI（gas/ai_review.py）
にレビューさせ、参考情報としてセーブ記録に添付する（生成失敗時もセーブ自体は
止めないベストエフォート運用。AIレビューが「リスク高」と示しても保存はブロック
しない）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.cloud import firestore
from firestore_client import db
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from auth.oauth import get_credentials_for

from .ai_review import generate_change_review
from .apps_script import fetch_source_files
from .audit import ACTION_RELEASE_REQUEST, log_operation
from .changes import detect_change, get_unreleased_changes, mark_changes_released
from .projects import get_project
from .savepoints import create_savepoint, get_latest_savepoint, get_savepoint_by_version


COLLECTION = "releases"


@dataclass
class ReleaseNotFoundError(Exception):
    """リリースまたはプロジェクトが存在しない。"""

    message: str


@dataclass
class NoChangesToReleaseError(Exception):
    """セーブ対象の未セーブ変更が存在しない。"""

    message: str


def create_release(project_id: str, requested_by: str, comment: str = "") -> dict[str, Any]:
    """未セーブの変更をまとめてセーブポイントとして記録する。"""
    project = get_project(project_id)
    if project is None:
        raise ReleaseNotFoundError(f"GASプロジェクトが見つかりません: {project_id}")

    previous_savepoint = get_latest_savepoint(project_id)
    previous_files = previous_savepoint["source_files"] if previous_savepoint else []

    # セーブ直前に現在のGASを再取得し、まだ検知していない駆け込み変更があれば検知しておく
    source_files = fetch_source_files(project["script_id"], get_credentials_for(project["google_account"]))
    detect_change(project_id, source_files)

    changes = get_unreleased_changes(project_id)
    if not changes:
        raise NoChangesToReleaseError(f"セーブ対象の未セーブ変更がありません: {project_id}")

    ai_review = _try_generate_review(project["project_name"], previous_files, source_files)

    savepoint = create_savepoint(
        project_id=project_id,
        source_files=source_files,
        comment=comment or "セーブ",
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
        "ai_review": ai_review,
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
    """指定プロジェクトのセーブポイント履歴を新しい順で取得する。"""
    query = db().collection(COLLECTION).where("project_id", "==", project_id)
    releases = [serialize_release(snapshot) for snapshot in query.stream()]
    return sorted(releases, key=lambda item: item.get("created_at") or "", reverse=True)


def get_release(release_id: str) -> dict[str, Any] | None:
    """セーブポイントを1件取得する。"""
    snapshot = db().collection(COLLECTION).document(release_id).get()
    if not snapshot.exists:
        return None
    return serialize_release(snapshot)


def regenerate_review(release_id: str) -> dict[str, Any]:
    """AIレビューの生成に失敗していたセーブポイントについて、再生成を試みる。"""
    release = get_release(release_id)
    if release is None:
        raise ReleaseNotFoundError(f"セーブポイントが見つかりません: {release_id}")

    project_id = release["project_id"]
    project = get_project(project_id)
    if project is None:
        raise ReleaseNotFoundError(f"GASプロジェクトが見つかりません: {project_id}")

    current_version_no = release["resulting_version_no"]
    current_savepoint = get_savepoint_by_version(project_id, current_version_no)
    previous_savepoint = get_savepoint_by_version(project_id, current_version_no - 1) if current_version_no > 1 else None
    current_files = current_savepoint["source_files"] if current_savepoint else []
    previous_files = previous_savepoint["source_files"] if previous_savepoint else []

    ai_review = _try_generate_review(project["project_name"], previous_files, current_files)
    db().collection(COLLECTION).document(release_id).update({"ai_review": ai_review})
    updated = get_release(release_id)
    if updated is None:
        raise ReleaseNotFoundError(f"セーブポイントが見つかりません: {release_id}")
    return updated


def _try_generate_review(
    project_name: str, previous_files: list[dict[str, Any]], current_files: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """AIレビューをベストエフォートで生成する（失敗してもセーブ自体は止めない）。"""
    try:
        review = generate_change_review(project_name, previous_files, current_files)
    except Exception:
        return None
    if review is None:
        return None
    review["generated_at"] = _now_iso()
    return review


def _now_iso() -> str:
    """現在時刻をISO文字列で返す（AIレビュー生成時刻の記録用）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def serialize_release(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    """Firestoreのセーブポイント文書をAPI用に整形する。"""
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data["created_at"] = _to_iso(data.get("created_at"))
    return data


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
