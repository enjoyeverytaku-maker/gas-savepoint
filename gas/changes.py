"""変更検知（パートナーズ版changes_admin相当の検知ログ部分）。

以前は変更ごとにレビュー(reviewed/changes_requested)・承認(approved)という
人間の手動ゲートを設けていたが、会長より「セーブポイントを作る際にそれまでの
変更の影響についてAIレビューしてくれるように」との指示を受け、個々の変更への
手動レビュー・承認は廃止した。以降このモジュールは「いつ・何が変わったか」を
検知して記録するだけの受動的なログであり、実際の影響レビューはセーブ
（gas/releases.py::create_release）時にAI（gas/ai_review.py）がまとめて行う。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.cloud import firestore
from firestore_client import db
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from .audit import ACTION_CHANGE_DETECT, log_operation
from .diff import calculate_diff, calculate_source_hash


COLLECTION = "changes"


@dataclass
class ChangeNotFoundError(Exception):
    """変更レコードが存在しない。"""

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
    """一覧表示用のステータス。"""
    return "released" if change.get("release_status") == "released" else "unreleased"


def serialize_change(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    """Firestoreの変更文書をAPI用に整形する。"""
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data["detected_at"] = _to_iso(data.get("detected_at"))
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
