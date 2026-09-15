import os
import threading
import time
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
def oauth_connect(actor: str = Depends(require_role("member"))):
    """ログイン中の本人が、自分のGoogleアカウントを接続する起点（F9）。

    2026-09-14再設計: パートナーズ版(multiuser_oauth.py)を精査した結果、「1つの代表アカウントが
    全GASを管理する」のではなく「GASを作った担当者それぞれが自分のアカウントで接続する」設計と
    判明したため、誰でも（member以上）自分自身のGoogleアカウントを接続できるようにした。
    """
    return RedirectResponse(oauth.build_auth_url(requested_by=actor))


def _authorization_response_url(request: Request) -> str:
    """OAuthコールバックのURLを、トークン交換に渡せる形で組み立てる。

    Cloud Run上ではTLSがフロントエンドで終端されコンテナへはHTTPで届くため、
    転送ヘッダーを信頼していないとrequest.urlのスキームがhttpになり、oauthlibが
    「OAuth 2 MUST utilize https.」で必ず失敗する（2026-09-15、萬年環境で実際に発生）。
    根本対処はDockerfileの--proxy-headers/--forwarded-allow-ips指定だが、将来その設定が
    失われても壊れないよう、Cloud Run上（K_SERVICEが必ず設定される）では明示的に
    httpsへ寄せる。ローカル開発（http://localhost）はこの分岐に入らないため影響しない。
    """
    url = request.url
    if os.environ.get("K_SERVICE") and url.scheme != "https":
        url = url.replace(scheme="https")
    return str(url)


@app.get("/oauth/callback")
def oauth_callback(request: Request):
    try:
        result = oauth.handle_callback(_authorization_response_url(request))
    except Exception as exc:  # noqa: BLE001 - ユーザー向けにエラー内容を返すため意図的に広く捕捉
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"connected": True, "email": result["email"]}


@app.get("/api/oauth/status")
def oauth_status():
    """ローカル開発時のRBACユーザーシミュレーション用（GAS接続状態とは無関係、2026-09-14分離）。

    SAVEPOINT_DEV_USER_EMAILで指定したメールアドレスを、SAVEPOINT_DEV_MODE時にX-Debug-User-Email
    ヘッダー代わりにブラウザ側へ伝える。複数アカウント対応後は「唯一の接続アカウント」という概念が
    無くなったため、以前のように接続済みGoogleアカウントを流用することはできない。
    """
    dev_mode = os.environ.get("SAVEPOINT_DEV_MODE") == "1"
    return {
        "dev_mode": dev_mode,
        "email": os.environ.get("SAVEPOINT_DEV_USER_EMAIL") if dev_mode else None,
    }


@app.get("/api/oauth/my-connection")
def oauth_my_connection(actor: str = Depends(require_role("member"))) -> dict[str, Any]:
    """ログイン中の本人自身のGoogleアカウント接続状況を返す（サイドバー・台帳画面表示用）。"""
    connected = oauth.is_account_connected(actor)
    return {"connected": connected, "email": actor if connected else None}


@app.get("/api/oauth/accounts")
def oauth_accounts(actor: str = Depends(require_role("member"))) -> list[dict[str, Any]]:
    """接続済みGoogleアカウント一覧を返す（台帳登録時にどのアカウントで操作するか選ぶ用）。"""
    return oauth.list_connected_accounts()


class UserUpsert(BaseModel):
    """ユーザー登録・更新リクエスト（F6）。"""

    email: str
    role: str


# LOGIN記録の間引き用（2026-09-15）。同じ利用者のログインはこの間隔を空けて記録する。
LOGIN_LOG_INTERVAL_SECONDS = 3600
_last_login_log: dict[str, float] = {}
_login_log_lock = threading.Lock()


def _should_log_login(email: str) -> bool:
    """この利用者のLOGINを今記録すべきか（直近に記録済みなら省く）。

    プロセス内の記録なので、インスタンスが増減すると同じ利用者が数回記録されることは
    あるが、毎アクセス記録されるのに比べれば十分少ない。監査ログの正確性より
    「操作履歴が埋もれないこと」を優先する割り切り。
    """
    now = time.monotonic()
    with _login_log_lock:
        last = _last_login_log.get(email)
        if last is not None and now - last < LOGIN_LOG_INTERVAL_SECONDS:
            return False
        _last_login_log[email] = now
        return True


@app.get("/api/me")
def get_me(request: Request) -> dict[str, Any]:
    """現在のリクエストの認証状態とロールを返す（画面側のボタン出し分けに使う）。

    このAPIは全画面が読み込みのたびに呼ぶため、毎回LOGINを記録すると監査ログが
    ログイン記録で埋まり、肝心の操作履歴（変更検知・セーブ・復元）が埋もれてしまう
    （2026-09-15、萬年環境で実際に発生。1画面表示につき2件記録されていた）。
    利用者ごとに一定時間は記録を省き、「誰がいつシステムを使ったか」の証跡としての
    役割は保ちつつノイズを減らす。
    """
    email = get_current_user_email(request)
    if email is None:
        return {"authenticated": False, "email": None, "role": None}
    role = get_user_role(email)
    if _should_log_login(email):
        log_operation(action=ACTION_LOGIN, user=email, result="success", details={"role": role})
    return {"authenticated": True, "email": email, "role": role}


def _app_base_url(request: Request) -> str:
    """招待メールに載せるアプリのURL。本番はAPP_BASE_URL（Cloud RunのIAP経由URL等）を優先し、
    未設定時はリクエストから推定する（uvicornにproxy-headers未設定だとscheme判定がhttpになりうるため暫定）。"""
    override = os.environ.get("APP_BASE_URL")
    if override:
        return override.rstrip("/")
    return str(request.base_url).rstrip("/")


def _try_send_invitation(email: str, role: str, app_base_url: str, sender_email: str) -> str | None:
    """招待メール送信をベストエフォートで行う（失敗してもユーザー登録自体は成功させる）。

    失敗理由を文字列で返す（成功時はNone）。2026-09-15修正: 以前は例外を握りつぶすだけで
    呼び出し元へ何も返しておらず、送信者が自分のGoogleアカウントを未接続だった場合などに
    メールが届いていないのに画面上は「追加しました」と表示され、管理者が気づけなかった。
    """
    try:
        send_invitation_email(email, role, app_base_url, sender_email)
    except Exception as exc:  # noqa: BLE001 - 失敗理由を画面へ返すため広く捕捉
        return str(exc)
    return None


@app.post("/api/users")
def post_user(body: UserUpsert, request: Request, actor: str = Depends(require_role("admin"))) -> dict[str, Any]:
    """ユーザーのロールを登録・更新する（管理者限定）。登録成功時、招待メールをベストエフォートで送信する
    （送信元は操作している管理者自身の接続済みGoogleアカウント）。"""
    try:
        user = upsert_user(email=body.email, role=body.role, updated_by=actor)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": {"type": "invalid_role", "message": str(exc)}})
    invitation_error = _try_send_invitation(user["email"], body.role, _app_base_url(request), actor)
    return {
        "ok": True,
        "user": user,
        "invitation_sent": invitation_error is None,
        "invitation_error": invitation_error,
    }


@app.post("/api/users/{email}/invite")
def post_user_invite(email: str, request: Request, actor: str = Depends(require_role("admin"))) -> dict[str, Any]:
    """招待メールを再送する（管理者限定）。送信元は操作している管理者自身の接続済みGoogleアカウント。"""
    user = get_user(email)
    if user is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": {"type": "not_found", "message": "ユーザーが見つかりません"}})
    try:
        send_invitation_email(email, user["role"], _app_base_url(request), actor)
    except Exception as exc:  # noqa: BLE001 - メール送信失敗の理由を画面へそのまま返すため広く捕捉
        return JSONResponse(status_code=200, content={"ok": False, "error": {"type": "invitation_failed", "message": str(exc)}})
    return {"ok": True}


@app.get("/api/users")
def get_users(request: Request, actor: str = Depends(require_role("admin"))) -> list[dict[str, Any]]:
    """ユーザー一覧を返す（Owner限定）。"""
    return list_users()


@app.get("/api/users/directory")
def get_users_directory(actor: str = Depends(require_role("member"))) -> list[dict[str, Any]]:
    """GASのメンバー追加時に選ぶための、登録済みユーザーの一覧（メールアドレスとロールのみ）。

    2026-09-15追加。以前は台帳画面のメンバー追加でメールアドレスを手入力させていたが、
    入力ミスがあっても登録自体は成功してしまい、権限が付いたつもりで付いていないという
    分かりにくい事故につながる。プロジェクトのメンバー管理はowner以上しか実行できないが、
    一覧の取得自体は画面表示のためmember以上に許可する（同一組織内の同僚のメールアドレスの
    みで、更新者や更新日時といった管理情報は返さない）。
    """
    return [{"email": user.get("email", ""), "role": user.get("role", "")} for user in list_users()]


@app.delete("/api/users/{email}")
def delete_user_route(email: str, request: Request, actor: str = Depends(require_role("admin"))) -> dict[str, Any]:
    """ユーザーを削除する（Owner限定）。"""
    delete_user(email)
    return {"ok": True}
