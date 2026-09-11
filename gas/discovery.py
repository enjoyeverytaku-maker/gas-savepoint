"""スタンドアロンGASの自動検出（F1拡張）。

スタンドアロン（スプレッドシート等に紐付かない独立した）GASプロジェクトは、
Google Drive上でmimeType="application/vnd.google-apps.script"のファイルとして
表れ、そのDriveファイルIDがそのままApps ScriptのScript IDになる。この性質を
利用してDrive APIで一覧を取得する。

バインドGAS（スプレッドシート等に紐付くGAS）はこの方法では検出できないため、
台帳画面から手動でScript IDを入力して登録する（gas/projects.create_project）。
"""
from __future__ import annotations

from typing import Any

import requests
from google.oauth2.credentials import Credentials

DRIVE_SCRIPT_MIME_TYPE = "application/vnd.google-apps.script"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"


def discover_standalone_projects(creds: Credentials) -> list[dict[str, Any]]:
    """接続済みGoogleアカウントが所有するスタンドアロンGASプロジェクトを一覧取得する。"""
    results: list[dict[str, Any]] = []
    page_token: str | None = None

    while True:
        params = {
            "q": f"mimeType='{DRIVE_SCRIPT_MIME_TYPE}' and trashed=false",
            "fields": "nextPageToken, files(id, name, owners(displayName, emailAddress))",
            "pageSize": 1000,
        }
        if page_token:
            params["pageToken"] = page_token

        response = requests.get(
            DRIVE_FILES_URL,
            headers={"Authorization": f"Bearer {creds.token}"},
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        for file in data.get("files", []):
            owners = file.get("owners") or []
            owner = owners[0] if owners else {}
            results.append(
                {
                    "script_id": file.get("id", ""),
                    "project_name": file.get("name", ""),
                    "owner_name": owner.get("displayName"),
                    "owner_email": owner.get("emailAddress"),
                }
            )

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return results
