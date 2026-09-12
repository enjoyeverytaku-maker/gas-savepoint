"""自動検知（F11、spec.md §5・§6）。

Cloud Scheduler経由で定期実行され、登録済み全GASプロジェクトの変更有無を
チェックする。パートナーズ版の実装を確認した結果、検知した変更は単なる
フラグではなく`changes`コレクションへ永続化し、レビュー・承認・リリースの
起点にしている（gas/changes.py参照）。フラグ（sync_status）は
ダッシュボード表示用の軽量なキャッシュとして引き続き保持する。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from google.cloud import firestore
from firestore_client import db

from auth.oauth import get_credentials
from auth.secrets import get_secret

from .apps_script import AppsScriptAPIError, fetch_source_files
from .changes import detect_change
from .projects import list_projects

SYNC_COLLECTION = "sync_status"
SCHEDULER_TOKEN_SECRET_ID = "savepoint-scheduler-token"
SCHEDULER_HEADER = "X-Scheduler-Token"


def verify_scheduler_token(request: Request) -> None:
    """Cloud Schedulerからの呼び出しであることを共有シークレットで検証する。

    人間のRBAC（auth/users.py）とは別の認証経路。Cloud SchedulerはIAP配下の
    ユーザーではないため、Secret Manager管理の固定トークンをヘッダーで照合する。
    """
    token = request.headers.get(SCHEDULER_HEADER)
    if not token or token != get_secret(SCHEDULER_TOKEN_SECRET_ID):
        raise HTTPException(status_code=401, detail="scheduler token invalid")


def check_project(project_id: str, script_id: str, creds: Any) -> dict[str, Any] | None:
    """1プロジェクト分の変更検知を行い、差分があればchangeを作成する。"""
    source_files = fetch_source_files(script_id, creds)
    change = detect_change(project_id, source_files)
    _write_status(project_id, has_changes=change is not None, changed_files=len(change["diff"]) if change else 0, error=None)
    return change


def check_all_projects() -> dict[str, Any]:
    """登録済み全GASプロジェクトの変更有無をチェックし、検知した変更を永続化する。"""
    projects = list_projects()
    creds = None
    checked = 0
    changed = 0
    errored = 0

    for project in projects:
        try:
            if creds is None:
                creds = get_credentials()
            change = check_project(project["id"], project["script_id"], creds)
            checked += 1
            if change is not None:
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
