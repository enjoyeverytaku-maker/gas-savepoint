from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import oauth
from gas.routes import router as gas_router

app = FastAPI(title="SavePoint")
app.include_router(gas_router)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/")
def dashboard(request: Request):
    """ダッシュボード画面（F10）。データはブラウザ側からJSON APIを呼んで描画する。"""
    return templates.TemplateResponse(request, "dashboard.html")


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
    return {"connected": connected, "email": oauth.get_connected_email() if connected else None}
