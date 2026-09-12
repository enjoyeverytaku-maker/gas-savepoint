"""変更検知・レビュー・承認（パートナーズ版changes_admin相当）。

パートナーズ版の実コードを確認した結果、GASの仕様上「セーブポイント」という
レビュー不要の即時保存は存在せず、実際には以下の一本のパイプラインだった:

  自動検知（sync）が変更を検知するたびに`changes`ドキュメントを作成
  → レビュー（reviewed / changes_requested）
  → 承認（reviewed済みのみ承認可）
  → リリース作成時、未リリースchangesが全件承認済みでなければブロック

これは「承認するまで本番反映を止める」機能ではなく（GASはエディタ保存時点で
即座に反映されるため技術的に不可能）、変更を正式な記録として認める前に
第三者レビューを挟む内部統制としての事後承認である。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from google.cloud import firestore
from firestore_client import db
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from .audit import ACTION_CHANGE_APPROVE, ACTION_CHANGE_DETECT, ACTION_CHANGE_REVIEW, log_operation
from .diff import calculate_diff, calculate_source_hash


COLLECTION = "changes"

REVIEW_STATUSES = ("unreviewed", "reviewed", "changes_requested")
APPROVAL_STATUSES = ("pending", "approved")


@dataclass
class ChangeNotFoundError(Exception):
    """変更レコードが存在しない。"""

    message: str


@dataclass
class ChangeNotReviewedError(Exception):
    """レビュー未完了の変更は承認できない。"""

    message: str


def get_baseline(project_id: str) -> tuple[list[dict[str, Any]], str | None]:
    """変更検知の比較基準（直近のchange、無ければ直近のリリース）を返す。"""
    from .savepoints import get_latest_savepoint

    latest_change = _get_latest_change(project_id)
    if latest_change is not None:
        return latest_change["source_files"], latest_change["source_hash"]

    latest_savepoint = get_latest_savepoint(project_id)
    if latest_savepoint is not None:
        return latest_savepoint["source_files"], latest_savepoint["source_hash"]

    return [], None


def detect_change(project_id: str, source_files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """現在のGASソースを比較基準と照合し、差分があれば新規changeを作成する。

    差分が無ければNoneを返す（重複したchangeを量産しないため）。
    """
    baseline_files, baseline_hash = get_baseline(project_id)
    current_hash = calculate_source_hash(source_files)
    if current_hash == baseline_hash:
        return None

    diff = calculate_diff(source_files, baseline_files)
    if not diff:
        return None

    payload = {
        "project_id": project_id,
        "source_files": source_files,
        "source_hash": current_hash,
        "previous_hash": baseline_hash,
        "diff": diff,
        "detected_at": SERVER_TIMESTAMP,
        "review_status": "unreviewed",
        "reviewed_by": None,
        "reviewed_at": None,
        "review_comment": None,
        "approval_status": "pending",
        "approved_by": None,
        "approved_at": None,
        "approval_comment": None,
        "release_status": "unreleased",
        "release_id": None,
    }
    _, doc_ref = db().collection(COLLECTION).add(payload)
    created = serialize_change(doc_ref.get())
    log_operation(action=ACTION_CHANGE_DETECT, project_id=project_id, user="system", result="success", details={"change_id": created["id"]})
    return created


def list_changes(project_id: str | None = None) -> list[dict[str, Any]]:
    """変更一覧を新しい順で返す（project_id省略時は全プロジェクト横断）。"""
    query = db().collection(COLLECTION)
    if project_id:
        query = query.where("project_id", "==", project_id)
    changes = [serialize_change(snapshot) for snapshot in query.stream()]
    return sorted(changes, key=lambda item: item.get("detected_at") or "", reverse=True)


def get_unreleased_changes(project_id: str) -> list[dict[str, Any]]:
    """指定プロジェクトの未リリースchangeを古い順で返す（リリース時のバンドル対象）。"""
    changes = [c for c in list_changes(project_id) if c.get("release_status") != "released"]
    return sorted(changes, key=lambda item: item.get("detected_at") or "")


def get_change(change_id: str) -> dict[str, Any] | None:
    """変更を1件取得する。"""
    snapshot = db().collection(COLLECTION).document(change_id).get()
    if not snapshot.exists:
        return None
    return serialize_change(snapshot)


def review_change(change_id: str, decision: str, comment: str, reviewed_by: str) -> dict[str, Any]:
    """変更をレビューする（reviewed / changes_requested）。

    changes_requestedの場合、過去の承認は無効化する（パートナーズ版と同様）。
    """
    if decision not in ("reviewed", "changes_requested"):
        raise ValueError(f"invalid review decision: {decision}")

    change = get_change(change_id)
    if change is None:
        raise ChangeNotFoundError(f"変更が見つかりません: {change_id}")

    update: dict[str, Any] = {
        "review_status": decision,
        "reviewed_by": reviewed_by,
        "reviewed_at": SERVER_TIMESTAMP,
        "review_comment": comment,
    }
    if decision == "changes_requested":
        update.update(
            {
                "approval_status": "pending",
                "approved_by": None,
                "approved_at": None,
                "approval_comment": None,
            }
        )
    db().collection(COLLECTION).document(change_id).update(update)
    updated = get_change(change_id)
    if updated is None:
        raise ChangeNotFoundError(f"変更が見つかりません: {change_id}")
    log_operation(action=ACTION_CHANGE_REVIEW, project_id=change["project_id"], user=reviewed_by, result=decision, details={"change_id": change_id, "comment": comment})
    return updated


def approve_change(change_id: str, comment: str, approved_by: str) -> dict[str, Any]:
    """レビュー済みの変更を承認する（reviewed以外は承認不可）。"""
    change = get_change(change_id)
    if change is None:
        raise ChangeNotFoundError(f"変更が見つかりません: {change_id}")
    if change.get("review_status") != "reviewed":
        raise ChangeNotReviewedError(f"レビュー済みになっていない変更は承認できません: {change_id}")

    db().collection(COLLECTION).document(change_id).update(
        {
            "approval_status": "approved",
            "approved_by": approved_by,
            "approved_at": SERVER_TIMESTAMP,
            "approval_comment": comment,
        }
    )
    updated = get_change(change_id)
    if updated is None:
        raise ChangeNotFoundError(f"変更が見つかりません: {change_id}")
    log_operation(action=ACTION_CHANGE_APPROVE, project_id=change["project_id"], user=approved_by, result="success", details={"change_id": change_id, "comment": comment})
    return updated


def mark_changes_released(change_ids: list[str], release_id: str) -> None:
    """バンドルされたchangesをリリース済みとして記録する。"""
    batch = db().batch()
    for change_id in change_ids:
        batch.update(
            db().collection(COLLECTION).document(change_id),
            {
                "release_status": "released",
                "release_id": release_id,
                "released_at": SERVER_TIMESTAMP,
            },
        )
    batch.commit()


def display_status(change: dict[str, Any]) -> str:
    """一覧・フィルタ表示用の統合ステータス。"""
    if change.get("release_status") == "released":
        return "released"
    if change.get("approval_status") == "approved":
        return "approved"
    if change.get("review_status") == "changes_requested":
        return "changes_requested"
    if change.get("review_status") == "reviewed":
        return "reviewed"
    return "unreviewed"


def serialize_change(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    """Firestoreの変更文書をAPI用に整形する。"""
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data["detected_at"] = _to_iso(data.get("detected_at"))
    data["reviewed_at"] = _to_iso(data.get("reviewed_at"))
    data["approved_at"] = _to_iso(data.get("approved_at"))
    data["released_at"] = _to_iso(data.get("released_at"))
    data["display_status"] = display_status(data)
    return data


def _get_latest_change(project_id: str) -> dict[str, Any] | None:
    """指定プロジェクトの最新change（状態を問わない）を取得する。"""
    changes = list_changes(project_id)
    return changes[0] if changes else None


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
