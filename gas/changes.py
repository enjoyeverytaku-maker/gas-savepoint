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

# 一覧表示に必要な軽量フィールド。source_files（GASソース全文）を含めないのが要点で、
# 画面の変更履歴はファイル名・増減行数・検知日時しか使わない（templates/changes.html参照）。
METADATA_FIELDS = [
    "project_id",
    "source_hash",
    "previous_hash",
    "diff",
    "detected_at",
    "release_status",
    "release_id",
    "released_at",
]


@dataclass
class ChangeNotFoundError(Exception):
    """変更レコードが存在しない。"""

    message: str


def get_baseline(project_id: str) -> tuple[list[dict[str, Any]], str | None]:
    """変更検知の比較基準（直近のchange、無ければ直近のセーブポイント）を返す。"""
    from .savepoints import get_latest_savepoint

    latest_change = _get_latest_change(project_id)
    if latest_change is not None:
        return latest_change.get("source_files") or [], latest_change.get("source_hash")

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

    # 保存する差分は行単位データを持たない（画面の変更履歴はファイル単位の増減行数しか
    # 使わない一方、行単位データを持たせるとソース本文と合わせてFirestoreの1ドキュメント
    # 上限1MiBを超えて保存自体が失敗しうるため、2026-09-15）。
    diff = calculate_diff(source_files, baseline_files, include_lines=False)
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


def list_changes(project_id: str | None = None, include_source: bool = False) -> list[dict[str, Any]]:
    """変更一覧を新しい順で返す（project_id省略時は全プロジェクト横断）。

    既定ではGASソース本文（source_files）を含めない。ダッシュボードと変更履歴画面が
    毎回この一覧を読むため、本文まで取得すると検知件数が増えるほど際限なく重くなる
    （2026-09-15、select()で必要フィールドだけ取得する方式へ変更）。
    """
    query = db().collection(COLLECTION)
    if project_id:
        query = query.where("project_id", "==", project_id)
    if not include_source:
        query = query.select(METADATA_FIELDS)
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
    """指定プロジェクトの最新change（状態を問わない）をソース本文込みで取得する。

    まず軽量なメタデータだけで最新の1件を特定し、本文はその1件だけを取りに行く
    （変更検知は定期実行で全プロジェクト分が繰り返し走るため、ここで過去の検知履歴を
    本文ごと読み込むと履歴が増えるほど毎回重くなっていた、2026-09-15）。
    """
    metadata = list_changes(project_id)
    if not metadata:
        return None
    snapshot = db().collection(COLLECTION).document(metadata[0]["id"]).get()
    if not snapshot.exists:
        return None
    return serialize_change(snapshot)


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
