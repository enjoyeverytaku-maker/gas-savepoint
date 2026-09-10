"""Apps Script API連携。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from google.oauth2.credentials import Credentials


APPS_SCRIPT_CONTENT_URL = "https://script.googleapis.com/v1/projects/{script_id}/content"


@dataclass
class AppsScriptAPIError(Exception):
    """Apps Script APIのエラー情報を保持する。"""

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
            message=_extract_error_message(response),
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
