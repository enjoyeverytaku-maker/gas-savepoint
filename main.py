from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from auth import oauth
from gas.routes import router as gas_router

app = FastAPI(title="SavePoint")
app.include_router(gas_router)


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
    return {"connected": oauth.is_connected()}
