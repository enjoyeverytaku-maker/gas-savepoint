"""GASセーブポイントCRUD。

2026-09-15最適化: 以前は「project_idで全セーブポイントを取得し、Python側で並べ替える」形に
なっており、最新1件を知りたいだけの場面（採番・差分の基準取得）でも過去の全バージョンの
ソースコード本体をFirestoreから丸ごとダウンロードしていた。セーブポイント1件はGASソース
全文を保持するため、履歴が増えるほど毎回の読み込み量が線形に増える構造だった。

- 一覧・採番のように本文が不要な場面ではFirestoreのselect()で必要なフィールドだけ取得する
- 本文が必要な場面（差分の基準・ロールバック対象）だけ、対象1件をドキュメント指定で取得する
- 採番はgas_projects側のカウンタをトランザクションで加算する方式へ変更（同時実行時に
  同じversion_noが二重採番されうる競合も併せて解消）
"""
from __future__ import annotations

from typing import Any

from google.cloud import firestore
from firestore_client import db
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from .audit import ACTION_VERSION_CREATE, log_operation
from .diff import calculate_source_hash


COLLECTION = "versions"
PROJECTS_COLLECTION = "gas_projects"

# 一覧表示・採番で必要な軽量フィールド（source_filesを含めないのが要点）。
METADATA_FIELDS = ["project_id", "version_no", "source_hash", "comment", "created_by", "created_at"]


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


def list_savepoints(project_id: str, include_source: bool = False) -> list[dict[str, Any]]:
    """指定プロジェクトのセーブポイント履歴を新しい順で取得する。

    既定ではソース本文（source_files）を含めない。画面の履歴一覧はバージョン番号・コメント・
    作成者・日時しか使わないため、本文まで取得すると履歴が増えた分だけ無駄に重くなる。
    差分計算など本文が要る場合のみinclude_source=Trueを指定する。
    """
    query = db().collection(COLLECTION).where("project_id", "==", project_id)
    if not include_source:
        query = query.select(METADATA_FIELDS)
    savepoints = [serialize_savepoint(snapshot) for snapshot in query.stream()]
    return sorted(savepoints, key=lambda item: item.get("version_no", 0), reverse=True)


def get_latest_savepoint(project_id: str) -> dict[str, Any] | None:
    """指定プロジェクトの最新セーブポイントを（ソース本文込みで）取得する。

    まず軽量なメタデータだけで最新版を特定し、本文はその1件だけを取りに行く。
    """
    metadata = list_savepoints(project_id)
    if not metadata:
        return None
    latest_id = metadata[0]["id"]
    snapshot = db().collection(COLLECTION).document(latest_id).get()
    if not snapshot.exists:
        return None
    return serialize_savepoint(snapshot)


def get_savepoint_by_version(project_id: str, version_no: int) -> dict[str, Any] | None:
    """指定バージョン番号のセーブポイントを取得する（ロールバック対象の特定に使う）。

    等価条件のみの複合クエリはFirestoreの単一フィールドインデックスで処理できるため、
    複合インデックスの事前作成は不要（order_byを併用しないのはそのため）。
    """
    query = (
        db()
        .collection(COLLECTION)
        .where("project_id", "==", project_id)
        .where("version_no", "==", int(version_no))
        .limit(1)
    )
    for snapshot in query.stream():
        return serialize_savepoint(snapshot)
    return None


def _next_version_no(project_id: str, client: firestore.Client) -> int:
    """指定プロジェクトの次のバージョン番号を採番する。

    gas_projects/{project_id}.latest_version_no をトランザクションで加算する。以前は
    全セーブポイントを読み出してmax+1していたため、(1)履歴が増えるほど重く、(2)同時に
    2件保存されると同じ番号を二重採番しうる競合があった（ロールバック時の自動バックアップと
    手動セーブが重なる場面が該当）。
    """
    project_ref = client.collection(PROJECTS_COLLECTION).document(project_id)
    transaction = client.transaction()
    return _allocate_version_no(transaction, project_ref, project_id, client)


@firestore.transactional
def _allocate_version_no(
    transaction: firestore.Transaction,
    project_ref: firestore.DocumentReference,
    project_id: str,
    client: firestore.Client,
) -> int:
    snapshot = project_ref.get(transaction=transaction)
    data = snapshot.to_dict() or {}
    current = data.get("latest_version_no")
    if current is None:
        # カウンタ導入前に作られたプロジェクトの移行パス（1プロジェクトにつき初回のみ）。
        current = _max_version_no_from_history(project_id, client)
    next_no = int(current) + 1
    transaction.set(project_ref, {"latest_version_no": next_no}, merge=True)
    return next_no


def _max_version_no_from_history(project_id: str, client: firestore.Client) -> int:
    """既存セーブポイントから最大バージョン番号を求める（カウンタ移行時のみ使う）。"""
    query = client.collection(COLLECTION).where("project_id", "==", project_id).select(["version_no"])
    version_nos = [int((snapshot.to_dict() or {}).get("version_no", 0)) for snapshot in query.stream()]
    return max(version_nos) if version_nos else 0


def serialize_savepoint(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    """Firestoreのセーブポイント文書をAPI用に整形する。"""
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data["created_at"] = _to_iso(data.get("created_at"))
    return data


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
