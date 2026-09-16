"""セーブ申請・承認・却下（パートナーズ版releases_admin相当、社内呼称「セーブポイント作成」）。

2026-09-16変更（会長指示・萬年様向け見積項目3「変更内容のレビュー・承認フロー」対応）:
一時期は人間の承認ゲートを廃止しAIレビューのみにしていたが、レビュー・承認フローを
正式に復活させた。Developer以上がセーブを「申請」し、Maintainer以上が内容を確認して
承認（実際にセーブポイントを作成）または却下する2段階フローになる。AIレビュー
（gas/ai_review.py）は引き続き申請時に生成し、承認者の判断材料として添付する
（AIレビュー自体は参考情報であり、承認・却下の判断はあくまで人間が行う）。
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
from .audit import ACTION_RELEASE_APPROVE, ACTION_RELEASE_REJECT, ACTION_RELEASE_REQUEST, log_operation
from .changes import detect_change, get_unreleased_changes, mark_changes_released
from .projects import get_project
from .savepoints import create_savepoint, get_latest_savepoint, get_savepoint_by_version


COLLECTION = "releases"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


@dataclass
class ReleaseNotFoundError(Exception):
    """リリースまたはプロジェクトが存在しない。"""

    message: str


@dataclass
class NoChangesToReleaseError(Exception):
    """セーブ対象の未セーブ変更が存在しない。"""

    message: str


@dataclass
class ReleaseNotPendingError(Exception):
    """承認待ち（pending）以外のリリース申請に対して承認・却下しようとした。"""

    message: str


def request_release(project_id: str, requested_by: str, comment: str = "") -> dict[str, Any]:
    """未セーブの変更をまとめてセーブ申請（pending）として記録する。

    この時点ではまだセーブポイントは作らない（gas_projectsの版数を消費しない）。
    Maintainer以上がapprove_release()で承認して初めてセーブポイントが作成される。
    """
    project = get_project(project_id)
    if project is None:
        raise ReleaseNotFoundError(f"GASプロジェクトが見つかりません: {project_id}")

    previous_savepoint = get_latest_savepoint(project_id)
    previous_files = previous_savepoint["source_files"] if previous_savepoint else []

    # 申請直前に現在のGASを再取得し、まだ検知していない駆け込み変更があれば検知しておく
    source_files = fetch_source_files(project["script_id"], get_credentials_for(project["google_account"]))
    detect_change(project_id, source_files)

    changes = get_unreleased_changes(project_id)
    if not changes:
        raise NoChangesToReleaseError(f"セーブ対象の未セーブ変更がありません: {project_id}")

    ai_review = _try_generate_review(project["project_name"], previous_files, source_files)

    change_ids = [c["id"] for c in changes]
    doc_ref = db().collection(COLLECTION).document()
    release_id = doc_ref.id

    payload = {
        "project_id": project_id,
        "status": STATUS_PENDING,
        "resulting_version_no": None,
        "change_ids": change_ids,
        "change_count": len(change_ids),
        "comment": comment,
        "ai_review": ai_review,
        "source_files": source_files,  # 承認時にこの内容でセーブポイントを作る（申請後の駆け込み変更を含めないため）
        "requested_by": requested_by,
        "created_at": SERVER_TIMESTAMP,
        "approved_by": None,
        "approved_at": None,
        "rejected_by": None,
        "rejected_at": None,
        "rejection_reason": None,
    }
    doc_ref.set(payload)

    log_operation(
        action=ACTION_RELEASE_REQUEST,
        project_id=project_id,
        user=requested_by,
        result="success",
        details={"release_id": release_id, "change_count": len(change_ids)},
    )
    return {"release": serialize_release(doc_ref.get())}


def approve_release(release_id: str, approved_by: str) -> dict[str, Any]:
    """承認待ちのセーブ申請を承認し、実際にセーブポイントを作成する。"""
    snapshot = db().collection(COLLECTION).document(release_id).get()
    if not snapshot.exists:
        raise ReleaseNotFoundError(f"セーブ申請が見つかりません: {release_id}")
    release = snapshot.to_dict() or {}
    if release.get("status") != STATUS_PENDING:
        raise ReleaseNotPendingError(f"承認待ち状態ではありません（現在の状態: {release.get('status')}）: {release_id}")

    project_id = release["project_id"]
    savepoint = create_savepoint(
        project_id=project_id,
        source_files=release["source_files"],
        comment=release.get("comment") or "セーブ",
        created_by=release["requested_by"],
    )
    db().collection(COLLECTION).document(release_id).update(
        {
            "status": STATUS_APPROVED,
            "resulting_version_no": int(savepoint["version_no"]),
            "approved_by": approved_by,
            "approved_at": SERVER_TIMESTAMP,
            "source_files": firestore.DELETE_FIELD,  # savepoint側に保存済みなので二重保持しない
        }
    )
    mark_changes_released(release["change_ids"], release_id=release_id)

    log_operation(
        action=ACTION_RELEASE_APPROVE,
        project_id=project_id,
        user=approved_by,
        target_version=int(savepoint["version_no"]),
        result="success",
        details={"release_id": release_id, "requested_by": release["requested_by"]},
    )
    updated = get_release(release_id)
    if updated is None:
        raise ReleaseNotFoundError(f"セーブ申請が見つかりません: {release_id}")
    return {"release": updated, "savepoint": savepoint}


def reject_release(release_id: str, rejected_by: str, reason: str = "") -> dict[str, Any]:
    """承認待ちのセーブ申請を却下する。セーブポイントは作られず、対象の変更は未セーブのまま残る
    （申請者が修正して再度申請できるようにするため）。
    """
    snapshot = db().collection(COLLECTION).document(release_id).get()
    if not snapshot.exists:
        raise ReleaseNotFoundError(f"セーブ申請が見つかりません: {release_id}")
    release = snapshot.to_dict() or {}
    if release.get("status") != STATUS_PENDING:
        raise ReleaseNotPendingError(f"承認待ち状態ではありません（現在の状態: {release.get('status')}）: {release_id}")

    db().collection(COLLECTION).document(release_id).update(
        {
            "status": STATUS_REJECTED,
            "rejected_by": rejected_by,
            "rejected_at": SERVER_TIMESTAMP,
            "rejection_reason": reason,
            "source_files": firestore.DELETE_FIELD,  # 却下後は不要（再申請時に最新状態を取り直す）
        }
    )

    log_operation(
        action=ACTION_RELEASE_REJECT,
        project_id=release["project_id"],
        user=rejected_by,
        result="success",
        details={"release_id": release_id, "requested_by": release["requested_by"], "reason": reason},
    )
    updated = get_release(release_id)
    if updated is None:
        raise ReleaseNotFoundError(f"セーブ申請が見つかりません: {release_id}")
    return updated


def list_releases(project_id: str) -> list[dict[str, Any]]:
    """指定プロジェクトのセーブ申請・セーブポイント履歴を新しい順で取得する（pending/approved/rejected全部）。"""
    query = db().collection(COLLECTION).where("project_id", "==", project_id)
    releases = [serialize_release(snapshot) for snapshot in query.stream()]
    return sorted(releases, key=lambda item: item.get("created_at") or "", reverse=True)


def get_release(release_id: str) -> dict[str, Any] | None:
    """セーブ申請/セーブポイントを1件取得する。"""
    snapshot = db().collection(COLLECTION).document(release_id).get()
    if not snapshot.exists:
        return None
    return serialize_release(snapshot)


def regenerate_review(release_id: str) -> dict[str, Any]:
    """AIレビューの生成に失敗していたセーブ申請/セーブポイントについて、再生成を試みる。"""
    release = get_release(release_id)
    if release is None:
        raise ReleaseNotFoundError(f"セーブ申請が見つかりません: {release_id}")

    project_id = release["project_id"]
    project = get_project(project_id)
    if project is None:
        raise ReleaseNotFoundError(f"GASプロジェクトが見つかりません: {project_id}")

    if release.get("status") == STATUS_PENDING:
        # 承認前はまだセーブポイントが無いため、申請時に取得済みのsource_filesを直接使う
        snapshot = db().collection(COLLECTION).document(release_id).get()
        current_files = (snapshot.to_dict() or {}).get("source_files", [])
        previous_savepoint = get_latest_savepoint(project_id)
        previous_files = previous_savepoint["source_files"] if previous_savepoint else []
    else:
        current_version_no = release["resulting_version_no"]
        current_savepoint = get_savepoint_by_version(project_id, current_version_no)
        previous_savepoint = get_savepoint_by_version(project_id, current_version_no - 1) if current_version_no > 1 else None
        current_files = current_savepoint["source_files"] if current_savepoint else []
        previous_files = previous_savepoint["source_files"] if previous_savepoint else []

    ai_review = _try_generate_review(project["project_name"], previous_files, current_files)
    db().collection(COLLECTION).document(release_id).update({"ai_review": ai_review})
    updated = get_release(release_id)
    if updated is None:
        raise ReleaseNotFoundError(f"セーブ申請が見つかりません: {release_id}")
    return updated


def _try_generate_review(
    project_name: str, previous_files: list[dict[str, Any]], current_files: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """AIレビューをベストエフォートで生成する（失敗しても申請・承認自体は止めない）。"""
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
    """Firestoreのセーブ申請/セーブポイント文書をAPI用に整形する。

    source_files（GASソース全文）は一覧・詳細表示に不要なため含めない
    （申請中はFirestore側にのみ持たせ、承認/却下と同時に削除する）。
    """
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data.pop("source_files", None)
    data.setdefault("status", STATUS_APPROVED)  # 2026-09-16以前に作られた旧データとの後方互換
    data["created_at"] = _to_iso(data.get("created_at"))
    data["approved_at"] = _to_iso(data.get("approved_at"))
    data["rejected_at"] = _to_iso(data.get("rejected_at"))
    return data


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
