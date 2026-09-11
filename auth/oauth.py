"""Google OAuth接続（F9、spec.md §11）。

単一組織（萬年）向けのInternalユーザータイプ接続を前提とし、既存プロトタイプの
multiuser_oauth.pyにあった複数組織選択ロジックは持ち込まない（クリーンリライト方針）。

前提となる事前設定（GCPコンソール側での手動作業、コードでは代替できない）:
  1. OAuth同意画面をInternalユーザータイプで作成する
  2. OAuth 2.0クライアントID（ウェブアプリケーション）を作成し、リダイレクトURIに
     OAUTH_REDIRECT_URI（例: https://<cloud-runのURL>/oauth/callback）を登録する
  3. 発行されたクライアントID・シークレットをSecret Managerへ登録する
     （シークレットID: savepoint-oauth-client-id / savepoint-oauth-client-secret）
"""
import os
import threading
from urllib.parse import parse_qs, urlparse

import requests
from google.auth.transport.requests import Request
from google.cloud import firestore
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from . import secrets

# script.projects はGAS本体の読み書きに必要な最小スコープ（読み取り専用に分離する場合は
# script.projects.readonlyへの変更を検討、docs/general_saas_roadmap.md §3参照）。
# drive.metadata.readonly + drive.scripts はスタンドアロンGAS自動検出（F1拡張）に必要
# （gas/discovery.pyがDrive APIでmimeType=application/vnd.google-apps.scriptのファイルを
# 検索する。既存プロトタイプのdiscover_apps_script_projects相当）
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.scripts",
]

OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8080/oauth/callback")
REFRESH_TOKEN_SECRET_ID = "savepoint-oauth-refresh-token"

_CONNECTION_DOC = ("google_account", "connection")

# get_credentials()のプロセス内キャッシュ。未キャッシュ・失効時のみSecret Manager経由の
# Refresh Token取得＋Googleへのアクセストークン更新を行う（登録プロジェクト数が増えるほど
# ダッシュボードが遅くなっていた問題への対処、2026-09-12）。
_credentials_cache: Credentials | None = None
_credentials_lock = threading.Lock()


def _db() -> firestore.Client:
    return firestore.Client(project=os.environ.get("GCP_PROJECT"))


def _client_config() -> dict:
    client_id = secrets.get_secret("savepoint-oauth-client-id")
    client_secret = secrets.get_secret("savepoint-oauth-client-secret")
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [OAUTH_REDIRECT_URI],
        }
    }


def build_auth_url() -> str:
    """OAuth同意画面へのリダイレクト先URLを生成する。

    /oauth/connectと/oauth/callbackは別々のHTTPリクエスト（別Flowインスタンス）になるため、
    PKCEのcode_verifierをFirestoreへ一時保存し、stateをキーにcallback側で復元する。
    """
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=OAUTH_REDIRECT_URI)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    _db().collection("oauth_flow_state").document(state).set(
        {"code_verifier": flow.code_verifier, "created_at": firestore.SERVER_TIMESTAMP}
    )
    return auth_url


def handle_callback(authorization_response_url: str) -> dict:
    """認可コードをトークンへ交換し、Refresh TokenをSecret Managerへ保存する。"""
    query = parse_qs(urlparse(authorization_response_url).query)
    state = query.get("state", [None])[0]
    state_ref = _db().collection("oauth_flow_state").document(state) if state else None
    state_doc = state_ref.get() if state_ref else None
    if not state_doc or not state_doc.exists:
        raise RuntimeError("OAuthのstateが見つかりませんでした。/oauth/connectからやり直してください")

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=OAUTH_REDIRECT_URI)
    flow.code_verifier = state_doc.to_dict()["code_verifier"]
    flow.fetch_token(authorization_response=authorization_response_url)
    state_ref.delete()
    creds = flow.credentials

    if not creds.refresh_token:
        raise RuntimeError(
            "refresh_tokenが取得できませんでした。既に同意済みの場合はGoogleアカウントの"
            "サードパーティアクセス設定からいったん連携を解除し、再接続してください"
            "（2回目以降のconsentではrefresh_tokenが返らないことがあるため）"
        )

    secrets.set_secret(REFRESH_TOKEN_SECRET_ID, creds.refresh_token)
    _invalidate_credentials_cache()

    email = _fetch_connected_email(creds)
    collection, doc_id = _CONNECTION_DOC
    _db().collection(collection).document(doc_id).set(
        {
            "email": email,
            "connected_at": firestore.SERVER_TIMESTAMP,
            "scopes": SCOPES,
        }
    )
    return {"email": email}


def _fetch_connected_email(creds: Credentials) -> str:
    resp = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("email", "")


def is_connected() -> bool:
    collection, doc_id = _CONNECTION_DOC
    return _db().collection(collection).document(doc_id).get().exists


def get_connected_email() -> str | None:
    """接続済みGoogleアカウントのメールアドレスを返す（ダッシュボードでの操作者表示用）。"""
    collection, doc_id = _CONNECTION_DOC
    snapshot = _db().collection(collection).document(doc_id).get()
    if not snapshot.exists:
        return None
    return (snapshot.to_dict() or {}).get("email")


def get_credentials() -> Credentials:
    """保存済みのRefresh Tokenから、Apps Script API呼び出し用のCredentialsを取得する。

    有効なアクセストークンをプロセス内にキャッシュし、失効するまで再利用する
    （キャッシュしないと、登録プロジェクト数分だけ毎回Secret Manager取得＋Googleへの
    トークンリフレッシュが並列発生し、プロジェクトが増えるほど遅くなっていた）。
    """
    global _credentials_cache
    with _credentials_lock:
        if _credentials_cache is not None and _credentials_cache.valid:
            return _credentials_cache

        refresh_token = secrets.get_secret(REFRESH_TOKEN_SECRET_ID)
        config = _client_config()["web"]
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=config["token_uri"],
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            scopes=SCOPES,
        )
        creds.refresh(Request())
        _credentials_cache = creds
        return creds


def _invalidate_credentials_cache() -> None:
    """再接続（スコープ変更等）でRefresh Tokenが更新された際にキャッシュを破棄する。"""
    global _credentials_cache
    with _credentials_lock:
        _credentials_cache = None
