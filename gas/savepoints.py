"""GASセーブポイントCRUD。"""
from __future__ import annotations

import os
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from .audit import ACTION_VERSION_CREATE, log_operation
from .diff import calculate_source_hash


COLLECTION = "versions"


def db() -> firestore.Client:
    """Firestoreクライアントを生成する。"""
    return firestore.Client(project=os.environ.get("GCP_PROJECT"))


def create_savepoint(
    project_id: str,
    source_files: list[dict[str, Any]],
    comment: str,
    created_by: str,
) -> dict[str, Any]:
    """GASソースを新しいセーブポイントとして保存する。"""
    client = db()
    version_no = _next_version_no(project_id, client)
    payload = {
        "project_id": project_id,
        "version_no": version_no,
        "source_files": source_files,
        "source_hash": calculate_source_hash(source_files),
        "comment": comment,
        "created_by": created_by,
        "created_at": SERVER_TIMESTAMP,
    }
    _, doc_ref = client.collection(COLLECTION).add(payload)
    created = serialize_savepoint(doc_ref.get())
    log_operation(
        action=ACTION_VERSION_CREATE,
        project_id=project_id,
        user=created_by,
        target_version=created["version_no"],
        result="success",
    )
    return created


def list_savepoints(project_id: str) -> list[dict[str, Any]]:
    """指定プロジェクトのセーブポイント履歴を新しい順で取得する。"""
    query = db().collection(COLLECTION).where("project_id", "==", project_id)
    savepoints = [serialize_savepoint(snapshot) for snapshot in query.stream()]
    return sorted(savepoints, key=lambda item: item.get("version_no", 0), reverse=True)


def get_latest_savepoint(project_id: str) -> dict[str, Any] | None:
    """指定プロジェクトの最新セーブポイントを取得する。"""
    savepoints = list_savepoints(project_id)
    return savepoints[0] if savepoints else None


def get_savepoint_by_version(project_id: str, version_no: int) -> dict[str, Any] | None:
    """指定バージョン番号のセーブポイントを取得する（ロールバック対象の特定に使う）。"""
    for savepoint in list_savepoints(project_id):
        if savepoint.get("version_no") == version_no:
            return savepoint
    return None


def _next_version_no(project_id: str, client: firestore.Client) -> int:
    """指定プロジェクトの次のバージョン番号を採番する。"""
    query = client.collection(COLLECTION).where("project_id", "==", project_id)
    snapshots = list(query.stream())
    if not snapshots:
        return 1
    return max(int((snapshot.to_dict() or {}).get("version_no", 0)) for snapshot in snapshots) + 1


def serialize_savepoint(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    """Firestoreのセーブポイント文書をAPI用に整形する。"""
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data["created_at"] = _to_iso(data.get("created_at"))
    return data


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
