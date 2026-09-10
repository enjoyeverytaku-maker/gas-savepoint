"""GAS管理APIルート。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from auth.oauth import get_credentials
from auth.users import require_role

from .apps_script import AppsScriptAPIError, fetch_source_files
from .audit import (
    ACTION_GAS_PROJECT_CREATE,
    ACTION_GAS_PROJECT_UPDATE,
    ACTION_SOURCE_FETCH,
    list_operations,
    log_operation,
)
from .diff import calculate_diff, calculate_source_hash
from .projects import create_project, get_project, list_projects, update_project
from .releases import (
    NoChangesToReleaseError,
    ReleaseNotFoundError,
    ReleaseNotPendingError,
    approve_release,
    create_release,
    list_releases,
    reject_release,
)
from .rollback import RollbackConflictError, RollbackTargetNotFoundError, rollback_to_version
from .savepoints import create_savepoint, get_latest_savepoint, list_savepoints
from .sync import check_all_projects, verify_scheduler_token


router = APIRouter(prefix="/api")


class ProjectCreate(BaseModel):
    """プロジェクト作成リクエスト。"""

    project_name: str
    script_id: str
    google_account: str | None = None
    description: str = ""
    status: str = ""
    department: str = ""


class ProjectUpdate(BaseModel):
    """台帳項目の更新リクエスト（F8）。指定したフィールドのみ更新する。"""

    project_name: str | None = None
    google_account: str | None = None
    description: str | None = None
    status: str | None = None
    department: str | None = None


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


class ReleaseCreate(BaseModel):
    """リリース申請リクエスト。"""

    requested_by: str = Field(default="unknown")
    comment: str = ""


class ReleaseDecision(BaseModel):
    """リリース承認・却下リクエスト。"""

    performed_by: str = Field(default="unknown")
    reason: str = ""


@router.post("/projects")
def post_project(project: ProjectCreate, actor: str = Depends(require_role("admin"))) -> dict[str, Any]:
    """GASプロジェクトを登録する（Admin限定）。"""
    created = create_project(project.model_dump())
    log_operation(action=ACTION_GAS_PROJECT_CREATE, project_id=created["id"], user=actor, result="success")
    return created


@router.get("/projects")
def get_projects(actor: str = Depends(require_role("viewer"))) -> list[dict[str, Any]]:
    """GASプロジェクト一覧を返す。"""
    return list_projects()


@router.patch("/projects/{project_id}")
def patch_project(project_id: str, body: ProjectUpdate, actor: str = Depends(require_role("admin"))):
    """台帳項目（用途・担当部署・ステータス等）を更新する（Admin限定、F8）。"""
    updated = update_project(project_id, body.model_dump())
    if updated is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": {"type": "not_found", "message": f"project not found: {project_id}"}})
    log_operation(action=ACTION_GAS_PROJECT_UPDATE, project_id=project_id, user=actor, result="success")
    return {"ok": True, "project": updated}


@router.get("/projects/{project_id}")
def get_project_detail(project_id: str, actor: str = Depends(require_role("viewer"))):
    """GASプロジェクト詳細を返す。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})
    return project


@router.post("/projects/{project_id}/fetch")
def fetch_project_source(project_id: str, actor: str = Depends(require_role("viewer"))):
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
    diff = calculate_diff(source_files, previous_files)
    log_operation(
        action=ACTION_SOURCE_FETCH,
        project_id=project_id,
        user=actor,
        result="success",
        details={"changed_files": len(diff)},
    )
    return {
        "ok": True,
        "project_id": project_id,
        "script_id": project["script_id"],
        "source_files": source_files,
        "source_hash": calculate_source_hash(source_files),
        "diff": diff,
    }


@router.post("/projects/{project_id}/savepoints")
def post_savepoint(project_id: str, savepoint: SavepointCreate, actor: str = Depends(require_role("editor"))):
    """GASソースを取得してセーブポイントを作成する（Editor以上）。"""
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
def get_savepoints(project_id: str, actor: str = Depends(require_role("viewer"))):
    """指定プロジェクトのセーブポイント履歴を返す。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})
    return list_savepoints(project_id)


@router.post("/projects/{project_id}/releases")
def post_release(project_id: str, body: ReleaseCreate, actor: str = Depends(require_role("editor"))):
    """GASソースを取得してリリース申請を作成する（Editor以上）。"""
    try:
        release = create_release(
            project_id=project_id,
            requested_by=body.requested_by,
            comment=body.comment,
        )
    except ReleaseNotFoundError as exc:
        return JSONResponse(status_code=404, content={"ok": False, "error": {"type": "not_found", "message": str(exc)}})
    except NoChangesToReleaseError as exc:
        return JSONResponse(
            status_code=200,
            content={"ok": False, "error": {"type": "no_changes", "message": str(exc)}},
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
    return {"ok": True, "release": release}


@router.get("/projects/{project_id}/releases")
def get_project_releases(project_id: str, actor: str = Depends(require_role("viewer"))):
    """指定プロジェクトのリリース申請一覧を返す。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": {"type": "not_found", "message": f"project not found: {project_id}"}},
        )
    return list_releases(project_id)


@router.post("/releases/{release_id}/approve")
def post_release_approve(release_id: str, body: ReleaseDecision, actor: str = Depends(require_role("admin"))):
    """リリース申請を承認する（Admin限定）。"""
    try:
        result = approve_release(release_id=release_id, approved_by=body.performed_by)
    except ReleaseNotFoundError as exc:
        return JSONResponse(status_code=404, content={"ok": False, "error": {"type": "not_found", "message": str(exc)}})
    except ReleaseNotPendingError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": {
                    "type": "already_decided",
                    "message": exc.message,
                    "current_status": exc.current_status,
                },
            },
        )
    return {"ok": True, **result}


@router.post("/releases/{release_id}/reject")
def post_release_reject(release_id: str, body: ReleaseDecision, actor: str = Depends(require_role("admin"))):
    """リリース申請を却下する（Admin限定）。"""
    try:
        release = reject_release(
            release_id=release_id,
            rejected_by=body.performed_by,
            reason=body.reason,
        )
    except ReleaseNotFoundError as exc:
        return JSONResponse(status_code=404, content={"ok": False, "error": {"type": "not_found", "message": str(exc)}})
    except ReleaseNotPendingError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": {
                    "type": "already_decided",
                    "message": exc.message,
                    "current_status": exc.current_status,
                },
            },
        )
    return {"ok": True, "release": release}


@router.post("/projects/{project_id}/rollback")
def post_rollback(project_id: str, body: RollbackRequest, actor: str = Depends(require_role("editor"))):
    """過去のセーブポイントへロールバックする（F4、Editor以上）。

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


@router.get("/audit-logs")
def get_audit_logs(actor: str = Depends(require_role("admin"))) -> list[dict[str, Any]]:
    """操作履歴（監査ログ）を新しい順で返す（Admin限定、F7）。"""
    return list_operations()


@router.post("/sync/check-all")
def post_sync_check_all(request: Request) -> dict[str, Any]:
    """Cloud Schedulerから定期実行され、全GASプロジェクトの変更有無をチェックする（F11）。

    人間のRBACとは別の認証経路（共有シークレットトークン）で保護する。
    """
    verify_scheduler_token(request)
    result = check_all_projects()
    return {"ok": True, **result}
