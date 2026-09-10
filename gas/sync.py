"""自動検知（F11、spec.md §5・§6）。

Cloud Scheduler経由で定期実行され、登録済み全GASプロジェクトの変更有無を
チェックし、結果をFirestoreのsync_statusコレクションへ保存する。
ユーザーが手動で「最新コードを取得」しなくても変更を検知できるようにする。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, Request
from google.cloud import firestore

from auth.oauth import get_credentials
from auth.secrets import get_secret

from .apps_script import AppsScriptAPIError, fetch_source_files
from .diff import calculate_diff
from .projects import list_projects
from .savepoints import get_latest_savepoint

SYNC_COLLECTION = "sync_status"
SCHEDULER_TOKEN_SECRET_ID = "savepoint-scheduler-token"
SCHEDULER_HEADER = "X-Scheduler-Token"


def db() -> firestore.Client:
    """Firestoreクライアントを生成する。"""
    return firestore.Client(project=os.environ.get("GCP_PROJECT"))


def verify_scheduler_token(request: Request) -> None:
    """Cloud Schedulerからの呼び出しであることを共有シークレットで検証する。

    人間のRBAC（auth/users.py）とは別の認証経路。Cloud SchedulerはIAP配下の
    ユーザーではないため、Secret Manager管理の固定トークンをヘッダーで照合する。
    """
    token = request.headers.get(SCHEDULER_HEADER)
    if not token or token != get_secret(SCHEDULER_TOKEN_SECRET_ID):
        raise HTTPException(status_code=401, detail="scheduler token invalid")


def check_all_projects() -> dict[str, Any]:
    """登録済み全GASプロジェクトの変更有無をチェックし、結果を保存する。"""
    projects = list_projects()
    creds = None
    checked = 0
    changed = 0
    errored = 0

    for project in projects:
        try:
            if creds is None:
                creds = get_credentials()
            source_files = fetch_source_files(project["script_id"], creds)
            latest_savepoint = get_latest_savepoint(project["id"])
            previous_files = latest_savepoint["source_files"] if latest_savepoint else []
            diff = calculate_diff(source_files, previous_files)
            has_changes = len(diff) > 0
            _write_status(project["id"], has_changes=has_changes, changed_files=len(diff), error=None)
            checked += 1
            if has_changes:
                changed += 1
        except AppsScriptAPIError as exc:
            _write_status(project["id"], has_changes=False, changed_files=0, error=exc.message)
            errored += 1
        except Exception as exc:  # noqa: BLE001 - 1件の失敗でバッチ全体を止めないため広く捕捉
            _write_status(project["id"], has_changes=False, changed_files=0, error=str(exc))
            errored += 1

    return {"total": len(projects), "checked": checked, "changed": changed, "errored": errored}


def get_sync_status(project_id: str) -> dict[str, Any] | None:
    """指定プロジェクトの最新の自動チェック結果を取得する。"""
    snapshot = db().collection(SYNC_COLLECTION).document(project_id).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    data["checked_at"] = _to_iso(data.get("checked_at"))
    return data


def _write_status(project_id: str, has_changes: bool, changed_files: int, error: str | None) -> None:
    """1プロジェクト分のチェック結果を保存する。"""
    db().collection(SYNC_COLLECTION).document(project_id).set(
        {
            "has_changes": has_changes,
            "changed_files": changed_files,
            "error": error,
            "checked_at": firestore.SERVER_TIMESTAMP,
        }
    )


def _to_iso(value: Any) -> Any:
    """日時値をISO文字列へ変換する。"""
    return value.isoformat() if hasattr(value, "isoformat") else value
