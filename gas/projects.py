"""GASプロジェクトCRUD。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from firestore_client import db
from google.cloud.firestore_v1 import SERVER_TIMESTAMP


COLLECTION = "gas_projects"
# script_idの重複登録を防ぐための索引コレクション（2026-09-15）。
# Firestoreには「あるフィールドが一意であること」を保証する仕組みが無いため、
# script_idから決まるドキュメントIDで索引を作り、create()（既存なら必ず失敗する）で
# 一意性を担保する。「先に検索して無ければ追加する」方式では、登録ボタンを連打した際の
# 同時リクエストをすり抜ける（実際に萬年環境で同一GASが12件登録される事故が発生した）。
INDEX_COLLECTION = "gas_project_script_ids"


@dataclass
class DuplicateScriptIdError(Exception):
    """同じスクリプトIDのGASが既に登録されている。"""

    script_id: str


def _index_id(script_id: str) -> str:
    """script_idから索引用の決定的なドキュメントIDを作る（IDに使えない文字を避けるためハッシュ化）。"""
    return hashlib.sha256(script_id.encode("utf-8")).hexdigest()[:40]


def create_project(data: dict[str, Any]) -> dict[str, Any]:
    """GASプロジェクトをFirestoreへ登録する。同じscript_idが既にあれば拒否する。

    google_accountは、そのGASの操作（差分取得・セーブ・ロールバック等）に使う接続済み
    Googleアカウントのメールアドレス（2026-09-14、パートナーズ版multiuser_oauth.pyを参考に
    「GASを作った担当者それぞれが自分のアカウントで接続する」設計へ変更。呼び出し元
    （gas/routes.py::post_project）で、実際に接続済みのアカウントであることを事前検証する）。
    """
    client = db()
    script_id = str(data["script_id"]).strip()
    if not script_id:
        raise ValueError("script_id is required")

    index_ref = client.collection(INDEX_COLLECTION).document(_index_id(script_id))
    try:
        # create()は既存ドキュメントがあると必ず失敗するため、同時リクエストでも
        # 1件しか通らない（add()やset()と違い上書きされない）。
        index_ref.create({"script_id": script_id, "created_at": SERVER_TIMESTAMP})
    except AlreadyExists:
        raise DuplicateScriptIdError(script_id=script_id)

    # 索引の導入（2026-09-15）より前に登録されたGASには索引が無いため、本体側も確認する。
    existing = (
        client.collection(COLLECTION).where("script_id", "==", script_id).limit(1).get()
    )
    if existing:
        index_ref.delete()
        raise DuplicateScriptIdError(script_id=script_id)

    payload = {
        "project_name": data["project_name"],
        "script_id": script_id,
        "google_account": data["google_account"],
        "description": data.get("description", ""),
        "status": data.get("status", ""),
        "department": data.get("department", ""),
        "created_at": SERVER_TIMESTAMP,
        "updated_at": SERVER_TIMESTAMP,
    }
    try:
        _, doc_ref = client.collection(COLLECTION).add(payload)
    except Exception:
        # 本体の作成に失敗した場合は索引を残さない（次回の登録を不可能にしないため）。
        index_ref.delete()
        raise
    index_ref.update({"project_id": doc_ref.id})
    return serialize_project(doc_ref.get())


def list_projects() -> list[dict[str, Any]]:
    """GASプロジェクト一覧を取得する（全件、フィルタなし）。"""
    query = db().collection(COLLECTION).order_by("created_at", direction=firestore.Query.DESCENDING)
    return [serialize_project(snapshot) for snapshot in query.stream()]


def list_projects_for_user(email: str, is_admin: bool) -> list[dict[str, Any]]:
    """ユーザーが閲覧可能なGASプロジェクトのみ返す。

    admin は全件。member はgas_projects/{id}/membersに個別付与されたプロジェクトのみ
    （2026-09-12、GASごとに権限を付与する方式へ変更）。

    2026-09-15最適化: 以前はプロジェクトごとにget_project_role()を呼び、その中で毎回
    「そのユーザーがadminか」をusersコレクションへ問い合わせていたため、プロジェクト数Nに対し
    2N回のFirestore往復が発生していた（呼び出し元は既にadminかどうかを知っているので重複判定）。
    メンバー権限の有無はget_all()で1往復にまとめる。
    """
    projects = list_projects()
    if is_admin:
        return projects

    from auth.users import normalize_email

    email = normalize_email(email)
    if not email or not projects:
        return []

    client = db()
    member_refs = [
        client.collection(COLLECTION).document(project["id"]).collection("members").document(email)
        for project in projects
    ]
    accessible_ids = {
        snapshot.reference.parent.parent.id
        for snapshot in client.get_all(member_refs)
        if snapshot.exists
    }
    return [project for project in projects if project["id"] in accessible_ids]


def get_project(project_id: str) -> dict[str, Any] | None:
    """GASプロジェクト詳細を取得する。"""
    snapshot = db().collection(COLLECTION).document(project_id).get()
    if not snapshot.exists:
        return None
    return serialize_project(snapshot)


EDITABLE_FIELDS = ("project_name", "google_account", "description", "status", "department")
# google_accountの更新（担当者の異動等）は、呼び出し元(gas/routes.py::patch_project)で
# 新しい値が実際に接続済みのアカウントであることを事前検証してから渡すこと。


def update_project(project_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """GASプロジェクトの台帳項目を更新する（F8）。script_idは登録後は変更しない（別プロジェクトとして再登録する）。"""
    doc_ref = db().collection(COLLECTION).document(project_id)
    if not doc_ref.get().exists:
        return None
    payload = {key: value for key, value in updates.items() if key in EDITABLE_FIELDS and value is not None}
    payload["updated_at"] = SERVER_TIMESTAMP
    doc_ref.update(payload)
    return serialize_project(doc_ref.get())


def set_readme(project_id: str, readme_markdown: str, summary: str = "") -> None:
    """AI生成READMEをプロジェクト文書へ保存する。

    summaryを渡した場合、台帳の「用途」(description)が空のときに限り書き込む
    （2026-09-15、会長提案。利用者が手入力した用途を勝手に上書きしないため、
    既に値が入っている場合は触らない）。
    """
    doc_ref = db().collection(COLLECTION).document(project_id)
    payload: dict[str, Any] = {
        "readme_markdown": readme_markdown,
        "readme_generated_at": SERVER_TIMESTAMP,
    }
    if summary:
        snapshot = doc_ref.get()
        current = (snapshot.to_dict() or {}).get("description", "") if snapshot.exists else ""
        if not str(current).strip():
            payload["description"] = summary
    doc_ref.update(payload)


def serialize_project(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    """Firestoreのプロジェクト文書をAPI用に整形する。"""
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    data["created_at"] = _to_iso(data.get("created_at"))
    data["updated_at"] = _to_iso(data.get("updated_at"))
    return data


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
