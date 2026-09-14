"""ユーザー招待メール（F6拡張）。

SavePointへのログインはIAP経由のGoogle Workspace SSOのため、パスワード発行や
アカウント作成用の招待リンクは不要——「アクセス権が付与されたので、このURLから
自分のGoogleアカウントでログインしてください」という通知メールを送るだけで完結する。

2026-09-14: 「システム専用の代表アカウント」という概念は増やさず、招待メールは
**操作している管理者本人**の接続済みGoogleアカウント（auth/oauth.py、gmail.send
スコープ込み）から送信する（GAS操作用の接続と同じ仕組みを流用）。そのため、招待メールを
送るには送信者自身が先に自分のGoogleアカウントを接続している必要がある。
"""
from __future__ import annotations

import base64
from email.mime.text import MIMEText

import requests

from auth.oauth import get_credentials_for

ROLE_LABELS = {"admin": "管理者", "member": "メンバー"}
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def send_invitation_email(to_email: str, role: str, app_base_url: str, sender_email: str) -> None:
    """招待メールを送信する。呼び出し元でベストエフォート運用（失敗してもユーザー登録自体は成功させる）にすること。

    sender_emailは送信操作をしている管理者自身のメールアドレス。そのアカウントが未接続の場合は
    get_credentials_forが例外を送出する（＝ベストエフォート呼び出し元でエラーメッセージとして拾われる）。
    """
    creds = get_credentials_for(sender_email)
    sender = sender_email
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
