import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from auth import oauth
from auth.users import (
    delete_user,
    get_current_user_email,
    get_user_role,
    list_users,
    require_role,
    upsert_user,
)
from gas.audit import ACTION_LOGIN, log_operation
from gas.routes import router as gas_router

app = FastAPI(title="SavePoint")
app.include_router(gas_router)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/")
def dashboard(request: Request):
    """ダッシュボード画面（F10）。データはブラウザ側からJSON APIを呼んで描画する。"""
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/audit-log")
def audit_log_page(request: Request):
    """監査ログ画面（F7）。閲覧権限はAPI側（/api/audit-logs、Admin限定）で強制する。"""
    return templates.TemplateResponse(request, "audit_log.html")


@app.get("/ledger")
def ledger_page(request: Request):
    """台帳画面（F8）。登録・編集の権限はAPI側（Admin限定）で強制する。"""
    return templates.TemplateResponse(request, "ledger.html")


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


@app.post("/api/users")
def post_user(body: UserUpsert, request: Request, actor: str = Depends(require_role("admin"))) -> dict[str, Any]:
    """ユーザーのロールを登録・更新する（Admin限定）。"""
    try:
        user = upsert_user(email=body.email, role=body.role, updated_by=actor)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": {"type": "invalid_role", "message": str(exc)}})
    return {"ok": True, "user": user}


@app.get("/api/users")
def get_users(request: Request, actor: str = Depends(require_role("admin"))) -> list[dict[str, Any]]:
    """ユーザー一覧を返す（Admin限定）。"""
    return list_users()


@app.delete("/api/users/{email}")
def delete_user_route(email: str, request: Request, actor: str = Depends(require_role("admin"))) -> dict[str, Any]:
    """ユーザーを削除する（Admin限定）。"""
    delete_user(email)
    return {"ok": True}
