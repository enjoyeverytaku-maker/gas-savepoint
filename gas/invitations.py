"""ユーザー招待メール（F6拡張）。

SavePointへのログインはIAP経由のGoogle Workspace SSOのため、パスワード発行や
アカウント作成用の招待リンクは不要——「アクセス権が付与されたので、このURLから
自分のGoogleアカウントでログインしてください」という通知メールを送るだけで完結する。
送信にはGASの操作に使っている接続済みGoogleアカウント（auth/oauth.py）にGmail送信
専用スコープ(gmail.send)を追加して利用する。
"""
from __future__ import annotations

import base64
from email.mime.text import MIMEText

import requests

from auth.oauth import get_connected_email, get_credentials

ROLE_LABELS = {"admin": "管理者", "member": "メンバー"}
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def send_invitation_email(to_email: str, role: str, app_base_url: str) -> None:
    """招待メールを送信する。呼び出し元でベストエフォート運用（失敗してもユーザー登録自体は成功させる）にすること。"""
    creds = get_credentials()
    sender = get_connected_email() or ""
    role_label = ROLE_LABELS.get(role, role)

    subject = "SavePointへのアクセス権が付与されました"
    body = f"""SavePointへのアクセス権が付与されました。

権限: {role_label}

以下のURLから、ご自身のGoogleアカウントでログインしてください。
{app_base_url}

このメールに心当たりが無い場合は、管理者にご確認ください。
"""
    message = MIMEText(body)
    message["to"] = to_email
    message["subject"] = subject
    if sender:
        message["from"] = sender

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    resp = requests.post(
        GMAIL_SEND_URL,
        headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
        json={"raw": raw},
        timeout=10,
    )
    resp.raise_for_status()
