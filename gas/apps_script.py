"""Apps Script API連携。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from google.oauth2.credentials import Credentials


APPS_SCRIPT_CONTENT_URL = "https://script.googleapis.com/v1/projects/{script_id}/content"


@dataclass
class AppsScriptAPIError(Exception):
    """Apps Script APIのエラー情報を保持する。

    messageは画面にそのまま表示される想定のため、日本語で「何が起きていて何をすればよいか」を入れる
    （利用者は非エンジニアで、Googleが返す英語の原文だけでは対処が分からないため、2026-09-15）。
    Googleからの原文はdetailsにそのまま残すので、調査時はそちらを参照する。
    """

    status_code: int
    message: str
    details: dict[str, Any] | None = None


def fetch_source_files(script_id: str, creds: Credentials) -> list[dict[str, str]]:
    """Apps Script APIからGASソース一式を取得する。"""
    response = requests.get(
        APPS_SCRIPT_CONTENT_URL.format(script_id=script_id),
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=30,
    )

    if not response.ok:
        raise AppsScriptAPIError(
            status_code=response.status_code,
            message=_user_message(response),
            details=_safe_json(response),
        )

    payload = response.json()
    return [
        {
            "name": file.get("name", ""),
            "type": file.get("type", ""),
            "source": file.get("source", ""),
        }
        for file in payload.get("files", [])
    ]


def update_content(script_id: str, source_files: list[dict[str, str]], creds: Credentials) -> None:
    """Apps Script APIでGAS本体のソースを全体置換する（ロールバック専用）。

    updateContentは差分パッチではなく全体置換のため、呼び出し側で必ず
    ロールバック前の自動バックアップ・楽観ロックを行うこと（gas/rollback.py参照）。
    """
    response = requests.put(
        APPS_SCRIPT_CONTENT_URL.format(script_id=script_id),
        headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
        json={
            "files": [
                {"name": f["name"], "type": f["type"], "source": f["source"]}
                for f in source_files
            ]
        },
        timeout=30,
    )

    if not response.ok:
        raise AppsScriptAPIError(
            status_code=response.status_code,
            message=_user_message(response),
            details=_safe_json(response),
        )


def _user_message(response: requests.Response) -> str:
    """Googleのエラー応答を、非エンジニアが読んで次の行動が分かる日本語の説明に変換する。

    該当する定型パターンが無い場合は原文をそのまま返す（推測で誤った案内をしないため）。
    """
    raw = _extract_error_message(response)
    lowered = raw.lower()
    status = response.status_code

    if "has not enabled the apps script api" in lowered:
        return (
            "このGASを操作するGoogleアカウントで「Google Apps Script API」が有効になっていません。"
            "そのアカウントで https://script.google.com/home/usersettings を開き、"
            "「Google Apps Script API」をオンにしてから数分待って、もう一度お試しください。"
        )
    if status == 401:
        return (
            "Googleアカウントの認可の期限が切れています。画面左下の「Googleアカウントを接続する」から"
            "接続し直してください。"
        )
    if status == 403:
        return (
            "このGASへのアクセス権がありません。台帳に登録したGoogleアカウントが、"
            "対象のGASを開ける権限を持っているかご確認ください。"
        )
    if status == 404:
        return (
            "対象のGASが見つかりません。台帳に登録したスクリプトIDが正しいか、"
            "そのGASが削除されていないかをご確認ください。"
        )
    if status == 429:
        return "Google側の利用上限に達したため、一時的に処理できませんでした。しばらく待ってからもう一度お試しください。"
    if status >= 500:
        return "Google側で一時的な障害が発生しています。しばらく待ってからもう一度お試しください。"
    return raw


def _extract_error_message(response: requests.Response) -> str:
    """APIレスポンスからエラーメッセージを取り出す。"""
    payload = _safe_json(response)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if payload.get("message"):
            return str(payload["message"])
    return response.text or "Apps Script API request failed"


def _safe_json(response: requests.Response) -> dict[str, Any] | None:
    """JSONレスポンスを安全に辞書へ変換する。"""
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None
