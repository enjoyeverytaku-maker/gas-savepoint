"""Google OAuth接続（F9、spec.md §11）。

2026-09-14再設計: パートナーズ版参照実装(multiuser_oauth.py/user_token_store.py)を精査した結果、
「1つの代表アカウントだけがGAS全体を管理する」設計ではなく、**GASを作った担当者それぞれが自分の
Googleアカウントで個別に接続し、そのGASの操作は登録者本人のトークンで行う**設計だったと判明。
萬年のように複数の担当者がそれぞれ自分でGASを作って管理している実態に合わせ、同じ設計を採用する。

- `google_accounts/{正規化したメールアドレス}` に、接続済みアカウントごとの情報を保存する
  （パートナーズ版のuser_token_store.pyと同じ発想。ただしSecret Manager側はハッシュ化した
  シークレットIDにリフレッシュトークンを1件ずつ保存し、Firestore側にはトークン本体を置かない）
- GASの台帳登録時に「どの接続済みアカウントで操作するか」(gas_projects.google_account)を選び、
  以降そのGASのApps Script API呼び出しは全てそのアカウントの認証情報で行う（gas/routes.py等の
  呼び出し側で get_credentials_for(project["google_account"]) を使う）
- ユーザー招待メール(gas/invitations.py)は「送信操作をしている管理者自身」の接続済みアカウントを
  使う。これによりメール送信専用の別アカウントという概念を増やさずに済む

前提となる事前設定（GCPコンソール側での手動作業、コードでは代替できない）:
  1. OAuth同意画面をInternalユーザータイプで作成する
  2. OAuth 2.0クライアントID（ウェブアプリケーション）を作成し、リダイレクトURIに
     OAUTH_REDIRECT_URI（例: https://<cloud-runのURL>/oauth/callback）を登録する
  3. 発行されたクライアントID・シークレットをSecret Managerへ登録する
     （シークレットID: savepoint-oauth-client-id / savepoint-oauth-client-secret）
"""
from __future__ import annotations

import hashlib
import os
import threading
from urllib.parse import parse_qs, urlparse

import requests
from google.auth.transport.requests import Request
from google.cloud import firestore
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from firestore_client import db as _shared_db
from . import secrets
from .users import normalize_email

# script.projects はGAS本体の読み書きに必要な最小スコープ（読み取り専用に分離する場合は
# script.projects.readonlyへの変更を検討、docs/general_saas_roadmap.md §3参照）。
# drive.metadata.readonly + drive.scripts はスタンドアロンGAS自動検出（F1拡張）に必要
# （gas/discovery.pyがDrive APIでmimeType=application/vnd.google-apps.scriptのファイルを
# 検索する。既存プロトタイプのdiscover_apps_script_projects相当）。
# gmail.send はユーザー招待メール（F6拡張）送信専用。受信・既存メールの読み取りは一切行わない
# 最小スコープ（gmail.readonly等は要求しない）。全スコープをまとめて1回の同意で要求する
# （GAS操作用と通知用で別々の接続フローを持たない、上記の設計統一による簡素化）。
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.scripts",
    "https://www.googleapis.com/auth/gmail.send",
]

OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8080/oauth/callback")

ACCOUNTS_COLLECTION = "google_accounts"
_SECRET_PREFIX = "savepoint-oauth-token-"

# get_credentials_for()のプロセス内キャッシュ（メールアドレスごと）。未キャッシュ・失効時のみ
# Secret Manager経由のRefresh Token取得＋Googleへのアクセストークン更新を行う
# （登録プロジェクト数が増えるほどダッシュボードが遅くなっていた問題への対処、2026-09-12。
# 複数アカウント対応後もアカウントごとにキャッシュして同じ理由で再取得を避ける）。
# dict自体の読み書きはGILのもとでアトミックなのでキャッシュ参照にロックは不要。
# _credentials_lockは「アカウントごとのロックを配るための辞書」を守るためだけに使う。
_credentials_cache: dict[str, Credentials] = {}
_account_locks: dict[str, threading.Lock] = {}
_credentials_lock = threading.Lock()


def _db() -> firestore.Client:
    # プロセス共有のFirestoreシングルトンを使う（firestore_client.py参照）。
    return _shared_db()


def _secret_id_for_email(email: str) -> str:
    """メールアドレスから決定的なSecret Manager用IDを作る（Secret IDには@や.を使えないためハッシュ化。
    パートナーズ版user_token_store.pyのlegacy_secret_idと同じ発想）。"""
    digest = hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()[:24]
    return f"{_SECRET_PREFIX}{digest}"


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


def build_auth_url(requested_by: str | None = None) -> str:
    """OAuth同意画面へのリダイレクト先URLを生成する。

    /oauth/connectと/oauth/callbackは別々のHTTPリクエスト（別Flowインスタンス）になるため、
    PKCEのcode_verifierをFirestoreへ一時保存し、stateをキーにcallback側で復元する。
    requested_byはSavePoint側でログイン中のユーザー（監査ログ用、任意）。接続されるGoogle
    アカウント自体はcallback側でuserinfoから取得した値を正とする（requested_byと一致するとは
    限らない。同じ人が別のGoogleアカウントを繋ぐケースもあり得るため）。
    """
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=OAUTH_REDIRECT_URI)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    _db().collection("oauth_flow_state").document(state).set(
        {
            "code_verifier": flow.code_verifier,
            "requested_by": requested_by,
            "created_at": firestore.SERVER_TIMESTAMP,
        }
    )
    return auth_url


def handle_callback(authorization_response_url: str) -> dict:
    """認可コードをトークンへ交換し、Refresh Tokenをそのアカウント専用のSecretへ保存する。"""
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

    email = normalize_email(_fetch_connected_email(creds))
    if not email:
        raise RuntimeError("接続したGoogleアカウントのメールアドレスを取得できませんでした")

    secrets.set_secret(_secret_id_for_email(email), creds.refresh_token)
    _invalidate_credentials_cache(email)

    _db().collection(ACCOUNTS_COLLECTION).document(email).set(
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


def is_account_connected(email: str) -> bool:
    email = normalize_email(email)
    if not email:
        return False
    return _db().collection(ACCOUNTS_COLLECTION).document(email).get().exists


def list_connected_accounts() -> list[dict]:
    """接続済みアカウント一覧を返す（台帳登録時のowner_email選択・管理画面表示用）。"""
    result = []
    for snapshot in _db().collection(ACCOUNTS_COLLECTION).stream():
        data = snapshot.to_dict() or {}
        connected_at = data.get("connected_at")
        result.append(
            {
                "email": data.get("email", snapshot.id),
                "connected_at": connected_at.isoformat() if hasattr(connected_at, "isoformat") else connected_at,
            }
        )
    return sorted(result, key=lambda a: a["email"])


def get_credentials_for(email: str) -> Credentials:
    """指定アカウントの保存済みRefresh Tokenから、Apps Script API呼び出し用のCredentialsを取得する。

    有効なアクセストークンをアカウントごとにプロセス内キャッシュし、失効するまで再利用する
    （キャッシュしないと、登録プロジェクト数分だけ毎回Secret Manager取得＋Googleへの
    トークンリフレッシュが並列発生し、プロジェクトが増えるほど遅くなっていた、2026-09-12）。

    2026-09-15最適化: ロックの粒度をアカウント単位に分けた。以前はグローバルロックを
    保持したままSecret Manager取得とGoogleへのトークン更新（ネットワークI/O・数百ms）を
    行っていたため、キャッシュ済みの別アカウントの読み取りまで待たされ、担当者ごとに
    アカウントが分かれる今の設計（＝リフレッシュ対象が複数）では全体が直列化していた。
    """
    email = normalize_email(email)
    if not email:
        raise ValueError("email is required")

    cached = _credentials_cache.get(email)
    if cached is not None and cached.valid:
        return cached

    # 同じアカウントへの同時リフレッシュだけを抑止する（別アカウントは並行して進める）。
    with _lock_for(email):
        cached = _credentials_cache.get(email)
        if cached is not None and cached.valid:
            return cached

        refresh_token = secrets.get_secret(_secret_id_for_email(email))
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
        _credentials_cache[email] = creds
        return creds


def _lock_for(email: str) -> threading.Lock:
    """アカウントごとのリフレッシュ用ロックを取得する（辞書自体の操作だけを短時間ロックする）。"""
    with _credentials_lock:
        lock = _account_locks.get(email)
        if lock is None:
            lock = threading.Lock()
            _account_locks[email] = lock
        return lock


def _invalidate_credentials_cache(email: str) -> None:
    """再接続（スコープ変更等）でRefresh Tokenが更新された際にそのアカウント分のキャッシュを破棄する。"""
    _credentials_cache.pop(normalize_email(email), None)
