"""GAS管理APIルート。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from auth.oauth import get_credentials

from .apps_script import AppsScriptAPIError, fetch_source_files
from .diff import calculate_diff, calculate_source_hash
from .projects import create_project, get_project, list_projects
from .rollback import RollbackConflictError, RollbackTargetNotFoundError, rollback_to_version
from .savepoints import create_savepoint, get_latest_savepoint, list_savepoints


router = APIRouter(prefix="/api")


class ProjectCreate(BaseModel):
    """プロジェクト作成リクエスト。"""

    project_name: str
    script_id: str
    google_account: str | None = None
    description: str = ""
    status: str = ""
    department: str = ""


class SavepointCreate(BaseModel):
    """セーブポイント作成リクエスト。"""

    comment: str = ""
    created_by: str = Field(default="unknown")


class RollbackRequest(BaseModel):
    """ロールバックリクエスト。"""

    target_version_no: int
    performed_by: str = Field(default="unknown")
    expected_current_hash: str | None = Field(
        default=None,
        description="クライアントが把握している現在の状態のハッシュ。楽観ロックに使用（省略も可だが推奨）",
    )


@router.post("/projects")
def post_project(project: ProjectCreate) -> dict[str, Any]:
    """GASプロジェクトを登録する。"""
    return create_project(project.model_dump())


@router.get("/projects")
def get_projects() -> list[dict[str, Any]]:
    """GASプロジェクト一覧を返す。"""
    return list_projects()


@router.get("/projects/{project_id}")
def get_project_detail(project_id: str):
    """GASプロジェクト詳細を返す。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})
    return project


@router.post("/projects/{project_id}/fetch")
def fetch_project_source(project_id: str):
    """最新GASソースを取得して差分を返す。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})

    try:
        source_files = fetch_source_files(project["script_id"], get_credentials())
    except AppsScriptAPIError as exc:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "error": {
                    "type": "apps_script_api_error",
                    "status_code": exc.status_code,
                    "message": exc.message,
                    "details": exc.details,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - 認証失敗もJSONで返すため広く捕捉
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "error": {
                    "type": "authentication_or_request_error",
                    "message": str(exc),
                },
            },
        )

    latest_savepoint = get_latest_savepoint(project_id)
    previous_files = latest_savepoint["source_files"] if latest_savepoint else []
    return {
        "ok": True,
        "project_id": project_id,
        "script_id": project["script_id"],
        "source_files": source_files,
        "source_hash": calculate_source_hash(source_files),
        "diff": calculate_diff(source_files, previous_files),
    }


@router.post("/projects/{project_id}/savepoints")
def post_savepoint(project_id: str, savepoint: SavepointCreate):
    """GASソースを取得してセーブポイントを作成する。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})

    try:
        source_files = fetch_source_files(project["script_id"], get_credentials())
    except AppsScriptAPIError as exc:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "error": {
                    "type": "apps_script_api_error",
                    "status_code": exc.status_code,
                    "message": exc.message,
                    "details": exc.details,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - 認証失敗もJSONで返すため広く捕捉
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "error": {
                    "type": "authentication_or_request_error",
                    "message": str(exc),
                },
            },
        )

    created = create_savepoint(
        project_id=project_id,
        source_files=source_files,
        comment=savepoint.comment,
        created_by=savepoint.created_by,
    )
    return {"ok": True, "savepoint": created}


@router.get("/projects/{project_id}/savepoints")
def get_savepoints(project_id: str):
    """指定プロジェクトのセーブポイント履歴を返す。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})
    return list_savepoints(project_id)


@router.post("/projects/{project_id}/rollback")
def post_rollback(project_id: str, body: RollbackRequest):
    """過去のセーブポイントへロールバックする（F4）。

    復元前に現在の状態を自動バックアップし、expected_current_hashが
    渡された場合は楽観ロックで同時編集を検知する。
    """
    try:
        result = rollback_to_version(
            project_id=project_id,
            target_version_no=body.target_version_no,
            performed_by=body.performed_by,
            expected_current_hash=body.expected_current_hash,
        )
    except RollbackTargetNotFoundError as exc:
        return JSONResponse(status_code=404, content={"ok": False, "error": {"type": "not_found", "message": str(exc)}})
    except RollbackConflictError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": {
                    "type": "conflict",
                    "message": "復元前にGASの状態が変わっています。最新の差分を確認してからやり直してください。",
                    "current_hash": exc.current_hash,
                    "expected_hash": exc.expected_hash,
                },
            },
        )
    except AppsScriptAPIError as exc:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "error": {
                    "type": "apps_script_api_error",
                    "status_code": exc.status_code,
                    "message": exc.message,
                    "details": exc.details,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - 認証失敗もJSONで返すため広く捕捉
        return JSONResponse(
            status_code=200,
            content={"ok": False, "error": {"type": "authentication_or_request_error", "message": str(exc)}},
        )

    return {"ok": True, **result}
