"""ユーザー権限管理（RBAC、F6、spec.md §13）。

SavePoint画面自体へのログイン認証は、本番環境ではCloud Run + IAP
（Identity-Aware Proxy）が担う想定（spec.md §11 TODO）。IAPは認証済み
ユーザーのメールアドレスをX-Goog-Authenticated-User-Emailヘッダーで
バックエンドへ渡すため、このモジュールはそのヘッダーを信頼して現在の
ユーザーを特定する。

ローカル開発環境（IAPが存在しない）では、環境変数SAVEPOINT_DEV_MODE=1を
設定した場合のみX-Debug-User-Emailヘッダーでのユーザー指定を許可する。
本番では絶対に有効化しないこと（OAUTHLIB_INSECURE_TRANSPORTと同種の
開発専用の抜け穴）。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, Request
from google.cloud import firestore
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

COLLECTION = "users"
IAP_HEADER = "X-Goog-Authenticated-User-Email"
DEV_HEADER = "X-Debug-User-Email"

# パートナーズ版（members_admin.py/global_members_admin.py）のロール名を踏襲した4段階。
# owner=プロジェクトの全操作、maintainer=owner相当だがユーザー管理は不可、
# developer=編集系操作、viewer=閲覧のみ（spec.md §13のAdmin/Editor/Viewerの3段階を
# 2026-09-12にこの4段階へ移行。旧admin→owner、旧editor→developerに相当）
ROLE_RANK = {"viewer": 0, "developer": 1, "maintainer": 2, "owner": 3}
VALID_ROLES = tuple(ROLE_RANK.keys())


def db() -> firestore.Client:
    """Firestoreクライアントを生成する。"""
    return firestore.Client(project=os.environ.get("GCP_PROJECT"))


def get_current_user_email(request: Request) -> str | None:
    """リクエストから認証済みユーザーのメールアドレスを取得する（未認証ならNone）。"""
    iap_value = request.headers.get(IAP_HEADER)
    if iap_value:
        return iap_value.split(":", 1)[-1]
    if os.environ.get("SAVEPOINT_DEV_MODE") == "1":
        debug_value = request.headers.get(DEV_HEADER)
        if debug_value:
            return debug_value
    return None


def get_user_role(email: str) -> str:
    """指定ユーザーのグローバルロールを返す。usersコレクションが1件も無い間は誰でもowner扱い（初回導入時のブートストラップ）。"""
    snapshot = db().collection(COLLECTION).document(email).get()
    if snapshot.exists:
        return (snapshot.to_dict() or {}).get("role", "viewer")
    if _is_bootstrap_state():
        return "owner"
    return "viewer"


def _is_bootstrap_state() -> bool:
    """usersコレクションが未作成（誰も登録されていない）かどうかを返す。"""
    return next(db().collection(COLLECTION).limit(1).stream(), None) is None


def require_role(min_role: str):
    """指定ロール以上を要求するFastAPI依存関数を返す。"""
    if min_role not in ROLE_RANK:
        raise ValueError(f"invalid role: {min_role}")

    def _dependency(request: Request) -> str:
        email = get_current_user_email(request)
        if email is None:
            raise HTTPException(status_code=401, detail="認証されていません")
        role = get_user_role(email)
        if ROLE_RANK.get(role, -1) < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"この操作には{min_role}以上の権限が必要です（現在のロール: {role}）",
            )
        return email

    return _dependency


def get_project_role(project_id: str, email: str) -> str:
    """指定プロジェクトでのユーザーのロールを返す（プロジェクト単位メンバーシップが優先、
    未設定ならグローバルロールにフォールバック。パートナーズ版members_admin.py相当）。"""
    snapshot = (
        db().collection("gas_projects").document(project_id).collection("members").document(email).get()
    )
    if snapshot.exists:
        return (snapshot.to_dict() or {}).get("role", "viewer")
    return get_user_role(email)


def require_project_role(min_role: str):
    """指定プロジェクトに対して指定ロール以上を要求するFastAPI依存関数を返す。

    パスパラメータ"project_id"を持つルートでのみ使用する。プロジェクト単位の
    メンバーシップ（gas_projects/{project_id}/members/{email}）があればそちらを
    優先し、無ければグローバルロール（require_roleと同じget_user_role）を使う。
    """
    if min_role not in ROLE_RANK:
        raise ValueError(f"invalid role: {min_role}")

    def _dependency(request: Request) -> str:
        email = get_current_user_email(request)
        if email is None:
            raise HTTPException(status_code=401, detail="認証されていません")
        project_id = request.path_params.get("project_id")
        if not project_id:
            raise RuntimeError("require_project_roleはproject_idパスパラメータを持つルートでのみ使用できます")
        role = get_project_role(project_id, email)
        if ROLE_RANK.get(role, -1) < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"この操作には{min_role}以上の権限が必要です（現在のロール: {role}）",
            )
        return email

    return _dependency


def upsert_user(email: str, role: str, updated_by: str) -> dict[str, Any]:
    """ユーザーのロールを登録・更新する。"""
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role}")
    db().collection(COLLECTION).document(email).set(
        {"email": email, "role": role, "updated_by": updated_by, "updated_at": SERVER_TIMESTAMP},
        merge=True,
    )
    return get_user(email)


def get_user(email: str) -> dict[str, Any] | None:
    """ユーザー1件を取得する。"""
    snapshot = db().collection(COLLECTION).document(email).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    data["updated_at"] = _to_iso(data.get("updated_at"))
    return data


def list_users() -> list[dict[str, Any]]:
    """全ユーザーを一覧取得する。"""
    result = []
    for snapshot in db().collection(COLLECTION).stream():
        data = snapshot.to_dict() or {}
        data["updated_at"] = _to_iso(data.get("updated_at"))
        result.append(data)
    return result


def delete_user(email: str) -> None:
    """ユーザーを削除する。"""
    db().collection(COLLECTION).document(email).delete()


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
