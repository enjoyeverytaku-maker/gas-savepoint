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
from firestore_client import db
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

COLLECTION = "users"
IAP_HEADER = "X-Goog-Authenticated-User-Email"
DEV_HEADER = "X-Debug-User-Email"

# グローバルロールは2段階のみ（2026-09-12〜）。admin=全GASプロジェクトへ自動的に
# owner相当でアクセス可能・ユーザー管理/監査ログ/台帳登録が可能。member=デフォルトでは
# どのGASプロジェクトへのアクセス権も持たず、gas/members.py（gas_projects/{id}/members）で
# プロジェクトごとに個別付与されて初めてアクセスできる。
GLOBAL_ROLE_RANK = {"member": 0, "admin": 1}
VALID_GLOBAL_ROLES = tuple(GLOBAL_ROLE_RANK.keys())
# 後方互換のためのエイリアス（旧コードがROLE_RANK/VALID_ROLESという名前を参照している場合用）
ROLE_RANK = GLOBAL_ROLE_RANK
VALID_ROLES = VALID_GLOBAL_ROLES

# プロジェクト単位のロールは従来通り4段階（パートナーズ版members_admin.py踏襲）。
# gas/members.pyのプロジェクトメンバーシップ、および本モジュールのget_project_roleが
# 返す値の語彙。グローバルロールとは別の語彙である点に注意。
PROJECT_ROLE_RANK = {"viewer": 0, "developer": 1, "maintainer": 2, "owner": 3}
VALID_PROJECT_ROLES = tuple(PROJECT_ROLE_RANK.keys())


def normalize_email(email: str) -> str:
    """メールアドレスをFirestoreのドキュメントIDとして使うための正規化（前後空白除去＋小文字化）。

    2026-09-15修正: 以前は正規化せずそのままドキュメントIDにしていたため、管理者が
    「Taro.Yamada@mannen.jp」で登録したユーザーにIAPが「taro.yamada@mannen.jp」を渡すと
    ロールが引けず、静かにmember扱い（＝権限なし）に落ちる不具合があった。Firestoreの
    ドキュメントIDは大文字小文字を区別するため、読み書き両方でここを通す。
    """
    return (email or "").strip().lower()


def get_current_user_email(request: Request) -> str | None:
    """リクエストから認証済みユーザーのメールアドレスを取得する（未認証ならNone）。

    SAVEPOINT_DEV_MODE時はヘッダーに加えクエリパラメータ(?debug_email=)も許可する
    （2026-09-14追記: /oauth/connectのような<a href>直接遷移ではfetch経由でしか付与できない
    X-Debug-User-Emailヘッダーを送れないため。本番ではSAVEPOINT_DEV_MODE自体が起動時に
    拒否される=main.py::_refuse_dev_mode_on_cloud_runため、このフォールバックが有効になるのは
    ローカル開発時のみ）。
    """
    iap_value = request.headers.get(IAP_HEADER)
    if iap_value:
        return normalize_email(iap_value.split(":", 1)[-1])
    if os.environ.get("SAVEPOINT_DEV_MODE") == "1":
        debug_value = request.headers.get(DEV_HEADER) or request.query_params.get("debug_email")
        if debug_value:
            return normalize_email(debug_value)
    return None


def get_user_role(email: str) -> str:
    """指定ユーザーのグローバルロールを返す。usersコレクションが1件も無い間は誰でもadmin扱い（初回導入時のブートストラップ）。"""
    snapshot = db().collection(COLLECTION).document(normalize_email(email)).get()
    if snapshot.exists:
        return (snapshot.to_dict() or {}).get("role", "member")
    if _is_bootstrap_state():
        return "admin"
    return "member"


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


def get_project_role(project_id: str, email: str) -> str | None:
    """指定プロジェクトでのユーザーのロールを返す（パートナーズ版members_admin.py相当）。

    グローバルadminは常に全プロジェクトへowner相当でアクセスできる。member は
    gas_projects/{project_id}/members/{email} に個別付与されていない限りNone
    （アクセス権なし）を返す——グローバルロールへの単純フォールバックは行わない
    （2026-09-12、GASごとに権限を付与する方式へ変更）。
    """
    if get_user_role(email) == "admin":
        return "owner"
    snapshot = (
        db()
        .collection("gas_projects")
        .document(project_id)
        .collection("members")
        .document(normalize_email(email))
        .get()
    )
    if snapshot.exists:
        return (snapshot.to_dict() or {}).get("role", "viewer")
    return None


def require_project_role(min_role: str):
    """指定プロジェクトに対して指定ロール以上を要求するFastAPI依存関数を返す。

    パスパラメータ"project_id"を持つルートでのみ使用する。ロールはget_project_role
    （グローバルadminは自動owner、それ以外はプロジェクト個別付与のみ）で決まる。
    """
    if min_role not in PROJECT_ROLE_RANK:
        raise ValueError(f"invalid role: {min_role}")

    def _dependency(request: Request) -> str:
        email = get_current_user_email(request)
        if email is None:
            raise HTTPException(status_code=401, detail="認証されていません")
        project_id = request.path_params.get("project_id")
        if not project_id:
            raise RuntimeError("require_project_roleはproject_idパスパラメータを持つルートでのみ使用できます")
        role = get_project_role(project_id, email)
        if role is None:
            raise HTTPException(status_code=403, detail="このGASプロジェクトへのアクセス権がありません（管理者に付与を依頼してください）")
        if PROJECT_ROLE_RANK[role] < PROJECT_ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"この操作には{min_role}以上の権限が必要です（現在のロール: {role}）",
            )
        return email

    return _dependency


def upsert_user(email: str, role: str, updated_by: str) -> dict[str, Any]:
    """ユーザーのグローバルロール（admin/member）を登録・更新する。"""
    if role not in VALID_GLOBAL_ROLES:
        raise ValueError(f"invalid role: {role}")
    email = normalize_email(email)
    if not email:
        raise ValueError("email is required")
    db().collection(COLLECTION).document(email).set(
        {"email": email, "role": role, "updated_by": updated_by, "updated_at": SERVER_TIMESTAMP},
        merge=True,
    )
    return get_user(email)


def get_user(email: str) -> dict[str, Any] | None:
    """ユーザー1件を取得する。"""
    snapshot = db().collection(COLLECTION).document(normalize_email(email)).get()
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
    db().collection(COLLECTION).document(normalize_email(email)).delete()


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
