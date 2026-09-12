import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from auth import oauth
from auth.users import (
    delete_user,
    get_current_user_email,
    get_user,
    get_user_role,
    list_users,
    require_role,
    upsert_user,
)
from gas.audit import ACTION_LOGIN, log_operation
from gas.invitations import send_invitation_email
from gas.routes import router as gas_router

def _refuse_dev_mode_on_cloud_run() -> None:
    """Cloud Run上（K_SERVICEが必ず設定される）で開発専用の抜け穴が有効なら起動を拒否する（spec.md §14）。

    SAVEPOINT_DEV_MODEはRBAC（auth/users.py）のX-Debug-User-Emailヘッダーを信頼してしまう
    抜け穴、OAUTHLIB_INSECURE_TRANSPORTはOAuthのHTTPS必須チェックを無効化する抜け穴で、
    いずれもローカル開発専用。本番でのデプロイミスによる有効化を起動時に強制的に防ぐ。
    """
    if not os.environ.get("K_SERVICE"):
        return
    if os.environ.get("SAVEPOINT_DEV_MODE") == "1":
        raise RuntimeError("SAVEPOINT_DEV_MODE must not be enabled on Cloud Run (K_SERVICE is set)")
    if os.environ.get("OAUTHLIB_INSECURE_TRANSPORT") == "1":
        raise RuntimeError("OAUTHLIB_INSECURE_TRANSPORT must not be enabled on Cloud Run (K_SERVICE is set)")


_refuse_dev_mode_on_cloud_run()

app = FastAPI(title="SavePoint")
app.include_router(gas_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/")
def dashboard(request: Request):
    """ダッシュボード画面（F10）。データはブラウザ側からJSON APIを呼んで描画する。"""
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/audit-log")
def audit_log_page(request: Request):
    """監査ログ画面（F7）。閲覧権限はAPI側（/api/audit-logs、Owner限定）で強制する。"""
    return templates.TemplateResponse(request, "audit_log.html")


@app.get("/ledger")
def ledger_page(request: Request):
    """台帳画面（F8）。登録・編集の権限はAPI側（Owner限定）で強制する。"""
    return templates.TemplateResponse(request, "ledger.html")


@app.get("/releases")
def releases_page(request: Request):
    """リリース管理画面（F5、パートナーズ版releases_admin相当の5画面構成、T21）。

    プロジェクト選択・新規リリース作成・一覧・詳細・サマリーをhashベースの
    クライアントサイドルーティングで1ファイルにまとめている。権限はAPI側
    （require_project_role）で強制する。
    """
    return templates.TemplateResponse(request, "releases.html")


@app.get("/changes")
def changes_page(request: Request):
    """変更履歴専用画面（パートナーズ版changes_admin相当、T22）。

    T10の監査ログ（誰が何を操作したか）とは別に、GASソースの差分そのものを
    バージョン単位の時系列で確認する画面。権限はAPI側（require_project_role）で強制する。
    """
    return templates.TemplateResponse(request, "changes.html")


@app.get("/users")
def users_page(request: Request):
    """グローバルユーザー管理画面（Owner限定、/api/usersはT9で実装済みだが対応画面が無かったギャップを解消）。

    閲覧・操作の権限はAPI側（require_role("admin")）で強制する。
    """
    return templates.TemplateResponse(request, "users.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/oauth/connect")
def oauth_connect():
    """管理者がGoogleアカウントを接続する起点（F9）。"""
    return RedirectResponse(oauth.build_auth_url())


@app.get("/oauth/callback")
def oauth_callback(request: Request):
    try:
        result = oauth.handle_callback(str(request.url))
    except Exception as exc:  # noqa: BLE001 - ユーザー向けにエラー内容を返すため意図的に広く捕捉
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"connected": True, "email": result["email"]}


@app.get("/api/oauth/status")
def oauth_status():
    connected = oauth.is_connected()
    return {
        "connected": connected,
        "email": oauth.get_connected_email() if connected else None,
        "dev_mode": os.environ.get("SAVEPOINT_DEV_MODE") == "1",
    }


class UserUpsert(BaseModel):
    """ユーザー登録・更新リクエスト（F6）。"""

    email: str
    role: str


@app.get("/api/me")
def get_me(request: Request) -> dict[str, Any]:
    """現在のリクエストの認証状態とロールを返す（画面側のボタン出し分けに使う）。呼び出し自体をLOGINとして記録する。"""
    email = get_current_user_email(request)
    if email is None:
        return {"authenticated": False, "email": None, "role": None}
    role = get_user_role(email)
    log_operation(action=ACTION_LOGIN, user=email, result="success", details={"role": role})
    return {"authenticated": True, "email": email, "role": role}


def _app_base_url(request: Request) -> str:
    """招待メールに載せるアプリのURL。本番はAPP_BASE_URL（Cloud RunのIAP経由URL等）を優先し、
    未設定時はリクエストから推定する（uvicornにproxy-headers未設定だとscheme判定がhttpになりうるため暫定）。"""
    override = os.environ.get("APP_BASE_URL")
    if override:
        return override.rstrip("/")
    return str(request.base_url).rstrip("/")


def _try_send_invitation(email: str, role: str, app_base_url: str) -> None:
    """招待メール送信をベストエフォートで行う（失敗してもユーザー登録自体は成功させる）。"""
    try:
        send_invitation_email(email, role, app_base_url)
    except Exception:
        pass


@app.post("/api/users")
def post_user(body: UserUpsert, request: Request, actor: str = Depends(require_role("admin"))) -> dict[str, Any]:
    """ユーザーのロールを登録・更新する（管理者限定）。登録成功時、招待メールをベストエフォートで送信する。"""
    try:
        user = upsert_user(email=body.email, role=body.role, updated_by=actor)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": {"type": "invalid_role", "message": str(exc)}})
    _try_send_invitation(body.email, body.role, _app_base_url(request))
    return {"ok": True, "user": user}


@app.post("/api/users/{email}/invite")
def post_user_invite(email: str, request: Request, actor: str = Depends(require_role("admin"))) -> dict[str, Any]:
    """招待メールを再送する（管理者限定）。"""
    user = get_user(email)
    if user is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": {"type": "not_found", "message": "ユーザーが見つかりません"}})
    try:
        send_invitation_email(email, user["role"], _app_base_url(request))
    except Exception as exc:  # noqa: BLE001 - メール送信失敗の理由を画面へそのまま返すため広く捕捉
        return JSONResponse(status_code=200, content={"ok": False, "error": {"type": "invitation_failed", "message": str(exc)}})
    return {"ok": True}


@app.get("/api/users")
def get_users(request: Request, actor: str = Depends(require_role("admin"))) -> list[dict[str, Any]]:
    """ユーザー一覧を返す（Owner限定）。"""
    return list_users()


@app.delete("/api/users/{email}")
def delete_user_route(email: str, request: Request, actor: str = Depends(require_role("admin"))) -> dict[str, Any]:
    """ユーザーを削除する（Owner限定）。"""
    delete_user(email)
    return {"ok": True}
