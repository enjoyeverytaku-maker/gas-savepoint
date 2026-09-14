"""GAS管理APIルート。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from auth.oauth import get_credentials_for, is_account_connected
from auth.users import (
    PROJECT_ROLE_RANK,
    get_current_user_email,
    get_project_role,
    get_user_role,
    require_project_role,
    require_role,
)

from .apps_script import AppsScriptAPIError, fetch_source_files
from .audit import (
    ACTION_GAS_PROJECT_CREATE,
    ACTION_GAS_PROJECT_UPDATE,
    ACTION_SOURCE_FETCH,
    list_operations,
    log_operation,
)
from .changes import list_changes
from .diff import calculate_diff, calculate_source_hash
from .discovery import discover_standalone_projects
from .members import list_members, remove_member, upsert_member
from .projects import create_project, get_project, list_projects, list_projects_for_user, set_readme, update_project
from .readme_gen import generate_readme
from .releases import (
    NoChangesToReleaseError,
    ReleaseNotFoundError,
    create_release,
    get_release,
    list_releases,
    regenerate_review,
)
from .rollback import RollbackConflictError, RollbackTargetNotFoundError, rollback_to_version
from .savepoints import get_latest_savepoint, list_savepoints
from .sync import check_all_projects, check_project, get_sync_status, verify_scheduler_token


router = APIRouter(prefix="/api")


class ProjectCreate(BaseModel):
    """プロジェクト作成リクエスト。

    google_accountは、このGASの操作（差分取得・セーブ・ロールバック等）に使う接続済み
    Googleアカウントのメールアドレス（2026-09-14、GASごとに担当者が個別に接続する設計へ変更）。
    """

    project_name: str
    script_id: str
    google_account: str
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


class MemberUpsert(BaseModel):
    """プロジェクトメンバー追加・更新リクエスト（F6拡張、パートナーズ版members_admin相当）。"""

    email: str
    role: str


@router.post("/projects")
def post_project(project: ProjectCreate, actor: str = Depends(require_role("member"))) -> dict[str, Any]:
    """GASプロジェクトを登録する（member以上、GASを作った担当者が自分で登録できる。
    2026-09-14: パートナーズ版multiuser_oauth.pyを参考に「代表アカウントが一括管理」から
    「担当者ごとに個別接続・登録」へ変更）。

    google_accountは自分自身の接続済みアカウントのみ指定可能（adminは代理登録のため、
    他の接続済みアカウントも指定できる）。未接続のアカウントを指定した場合はエラーで拒否する。
    登録者本人（管理者以外）は、登録と同時にそのプロジェクトのowner権限を自動付与する
    （デフォルトアクセス権なしのRBACのもとで、自分が登録したGASに自分が触れなくなるのを防ぐ）。
    """
    is_admin = get_user_role(actor) == "admin"
    if project.google_account != actor and not is_admin:
        raise HTTPException(status_code=403, detail="自分自身が接続したGoogleアカウントのみ指定できます")
    if not is_account_connected(project.google_account):
        raise HTTPException(
            status_code=400,
            detail=f"{project.google_account} はまだGoogleアカウントが接続されていません。先に画面から『Googleアカウントを接続する』を行ってください。",
        )

    created = create_project(project.model_dump())
    if not is_admin:
        upsert_member(created["id"], actor, "owner", updated_by=actor)
    log_operation(action=ACTION_GAS_PROJECT_CREATE, project_id=created["id"], user=actor, result="success")
    try:
        source_files = fetch_source_files(created["script_id"], get_credentials_for(created["google_account"]))
        _try_generate_and_save_readme(
            project_id=created["id"],
            project_name=created["project_name"],
            source_files=source_files,
        )
    except Exception:
        pass
    return created


@router.get("/projects")
def get_projects(actor: str = Depends(require_role("member"))) -> list[dict[str, Any]]:
    """GASプロジェクト一覧を返す（memberは個別付与されたプロジェクトのみ、adminは全件）。"""
    return list_projects_for_user(actor, get_user_role(actor) == "admin")


@router.get("/projects/{project_id}/sync-status")
def get_project_sync_status(project_id: str, actor: str = Depends(require_project_role("viewer"))) -> dict[str, Any]:
    """直近の自動/手動チェック結果をFirestoreキャッシュから返す（Apps Script APIを呼ばない軽量版）。

    ダッシュボード初期表示を高速化するため、ページ読み込み時はこちらを使い、
    最新化したい場合のみユーザー操作でsync-check（生きたAPI呼び出し）を行う
    （2026-09-12、毎回全プロジェクトへ生きたAPI呼び出しをしていた読み込み遅延を解消）。
    """
    status = get_sync_status(project_id)
    return status or {"has_changes": None, "changed_files": 0, "error": None, "checked_at": None}


@router.get("/projects/{project_id}/my-role")
def get_my_project_role(project_id: str, actor: str = Depends(require_role("member"))) -> dict[str, Any]:
    """現在のユーザーの指定プロジェクトでのロールを返す（アクセス権が無ければroleはnull、UI側の編集可否判定用）。"""
    return {"role": get_project_role(project_id, actor)}


@router.get("/discovery/standalone")
def get_discovery_standalone(actor: str = Depends(require_role("member"))) -> dict[str, Any]:
    """自分自身が接続したGoogleアカウントのスタンドアロンGASを自動検出する（F1拡張）。

    2026-09-14: 誰でも自分の接続済みアカウントで検出できるよう変更（member以上）。
    バインドGAS（スプレッドシート等に紐付くGAS）はこの方法では検出できないため対象外。
    台帳画面からScript IDを手動入力して登録する。
    """
    if not is_account_connected(actor):
        return {"ok": False, "error": {"type": "not_connected", "message": "先に画面から『Googleアカウントを接続する』を行ってください。"}}
    try:
        discovered = discover_standalone_projects(get_credentials_for(actor))
    except Exception as exc:  # noqa: BLE001 - Drive API呼び出し失敗もJSONで返す
        return {"ok": False, "error": {"type": "discovery_error", "message": str(exc)}}

    registered_script_ids = {project["script_id"] for project in list_projects()}
    for item in discovered:
        item["already_registered"] = item["script_id"] in registered_script_ids
    return {"ok": True, "projects": discovered}


@router.patch("/projects/{project_id}")
def patch_project(project_id: str, body: ProjectUpdate, actor: str = Depends(require_project_role("owner"))):
    """台帳項目（用途・担当部署・ステータス等）を更新する（プロジェクトOwner以上、F8）。

    google_account（担当者の異動等での付け替え）は、変更後の値が実際に接続済みであることを
    事前検証する（2026-09-14）。
    """
    if body.google_account is not None and not is_account_connected(body.google_account):
        raise HTTPException(
            status_code=400,
            detail=f"{body.google_account} はまだGoogleアカウントが接続されていません。先にその担当者に『Googleアカウントを接続する』を行ってもらってください。",
        )
    updated = update_project(project_id, body.model_dump())
    if updated is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": {"type": "not_found", "message": f"project not found: {project_id}"}})
    log_operation(action=ACTION_GAS_PROJECT_UPDATE, project_id=project_id, user=actor, result="success")
    return {"ok": True, "project": updated}


@router.get("/projects/{project_id}")
def get_project_detail(project_id: str, actor: str = Depends(require_project_role("viewer"))):
    """GASプロジェクト詳細を返す。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})
    return project


@router.post("/projects/{project_id}/fetch")
def fetch_project_source(project_id: str, actor: str = Depends(require_project_role("viewer"))):
    """最新GASソースを取得して差分を返す。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})

    try:
        source_files = fetch_source_files(project["script_id"], get_credentials_for(project["google_account"]))
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


@router.post("/projects/{project_id}/sync-check")
def post_sync_check(project_id: str, actor: str = Depends(require_project_role("developer"))):
    """1プロジェクト分の変更検知を今すぐ実行する（定期実行を待たずに検知したい場合、プロジェクトDeveloper以上）。

    パートナーズ版に「レビュー不要の即時保存（セーブポイント）」は存在しないため、
    このエンドポイントは変更の検知（gas/changes.py）のみを行う。正式な記録には
    レビュー・承認・リリース作成が必要。
    """
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})

    try:
        change = check_project(project_id, project["script_id"], get_credentials_for(project["google_account"]))
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
    return {"ok": True, "change": change}


def _require_release_project_role(request: Request, release_id: str, min_role: str) -> str:
    """リリースが属するプロジェクトに対して指定ロール以上を要求する。

    /releases/{release_id}/... にはproject_idがパスに含まれないため、
    require_project_roleのDependsパターンが使えない。先にreleaseを引いて
    project_idを解決してから権限判定する。
    """
    email = get_current_user_email(request)
    if email is None:
        raise HTTPException(status_code=401, detail="認証されていません")
    release = get_release(release_id)
    if release is None:
        raise HTTPException(status_code=404, detail=f"リリース申請が見つかりません: {release_id}")
    role = get_project_role(release["project_id"], email)
    if role is None:
        raise HTTPException(status_code=403, detail="このGASプロジェクトへのアクセス権がありません（管理者に付与を依頼してください）")
    if PROJECT_ROLE_RANK[role] < PROJECT_ROLE_RANK[min_role]:
        raise HTTPException(
            status_code=403,
            detail=f"この操作には{min_role}以上の権限が必要です（現在のロール: {role}）",
        )
    return email


def _try_generate_and_save_readme(project_id: str, project_name: str, source_files: list[dict[str, Any]]) -> None:
    """README生成と保存をベストエフォートで実行する。"""
    try:
        readme_markdown = generate_readme(project_name, source_files)
        set_readme(project_id, readme_markdown)
    except Exception:
        pass


@router.get("/projects/{project_id}/savepoints")
def get_savepoints(project_id: str, actor: str = Depends(require_project_role("viewer"))):
    """指定プロジェクトのセーブポイント履歴を返す。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})
    return list_savepoints(project_id)


@router.get("/projects/{project_id}/changes")
def get_project_changes(project_id: str, actor: str = Depends(require_project_role("viewer"))):
    """指定プロジェクトの変更検知ログを返す（パートナーズ版changes_admin相当、T22）。

    自動検知（sync）が作成した変更を検知日時・セーブ状態つきで一覧表示する
    受動的なログ。影響レビューはAIがセーブ時にまとめて行う（gas/ai_review.py）ため、
    ここに人間のレビュー・承認操作は無い。T10の監査ログ（誰が何を操作したか）とは別。
    """
    project = get_project(project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project not found"})
    return list_changes(project_id)


@router.get("/changes")
def get_all_changes(actor: str = Depends(require_role("member"))):
    """全プロジェクト横断の変更検知ログを返す（パートナーズ版changes_admin相当）。memberはアクセス権のあるプロジェクト分のみ。"""
    if get_user_role(actor) == "admin":
        return list_changes()
    accessible_ids = {p["id"] for p in list_projects_for_user(actor, is_admin=False)}
    return [c for c in list_changes() if c.get("project_id") in accessible_ids]


@router.get("/projects/{project_id}/members")
def get_project_members(project_id: str, actor: str = Depends(require_project_role("viewer"))) -> list[dict[str, Any]]:
    """指定プロジェクトのメンバー一覧を返す（パートナーズ版members_admin相当、F6拡張）。"""
    return list_members(project_id)


@router.post("/projects/{project_id}/members")
def post_project_member(project_id: str, body: MemberUpsert, actor: str = Depends(require_project_role("owner"))):
    """プロジェクトへメンバーを追加・ロール更新する（プロジェクトOwner限定）。"""
    try:
        member = upsert_member(project_id, body.email, body.role, updated_by=actor)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": {"type": "invalid_role", "message": str(exc)}})
    return {"ok": True, "member": member}


@router.delete("/projects/{project_id}/members/{email}")
def delete_project_member(project_id: str, email: str, actor: str = Depends(require_project_role("owner"))):
    """プロジェクトからメンバーを削除する（プロジェクトOwner限定。削除後はグローバルロールへフォールバックする）。"""
    remove_member(project_id, email)
    return {"ok": True}


@router.post("/projects/{project_id}/releases")
def post_release(project_id: str, body: ReleaseCreate, actor: str = Depends(require_project_role("developer"))):
    """未セーブの変更をまとめてセーブポイントとして記録する（プロジェクトDeveloper以上）。

    人間による承認ゲートは無く、代わりに前回セーブポイントからの累積差分を
    AIがレビューし参考情報として記録に添付する（gas/releases.py参照）。
    """
    try:
        result = create_release(
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
    return {"ok": True, "release": result["release"]}


@router.get("/projects/{project_id}/releases")
def get_project_releases(project_id: str, actor: str = Depends(require_project_role("viewer"))):
    """指定プロジェクトのリリース申請一覧を返す。"""
    project = get_project(project_id)
    if project is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": {"type": "not_found", "message": f"project not found: {project_id}"}},
        )
    return list_releases(project_id)


@router.get("/releases/{release_id}")
def get_release_detail(release_id: str, request: Request):
    """セーブポイント1件の詳細を返す（T21セーブポイント詳細画面向け）。"""
    _require_release_project_role(request, release_id, "viewer")
    release = get_release(release_id)
    if release is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": {"type": "not_found", "message": f"セーブポイントが見つかりません: {release_id}"}})
    return release


@router.post("/releases/{release_id}/regenerate-review")
def post_regenerate_review(release_id: str, request: Request):
    """AIレビューの生成に失敗していたセーブポイントについて、再生成を試みる（対象プロジェクトDeveloper以上）。"""
    _require_release_project_role(request, release_id, "developer")
    try:
        release = regenerate_review(release_id)
    except ReleaseNotFoundError as exc:
        return JSONResponse(status_code=404, content={"ok": False, "error": {"type": "not_found", "message": str(exc)}})
    return {"ok": True, "release": release}


@router.post("/projects/{project_id}/rollback")
def post_rollback(project_id: str, body: RollbackRequest, actor: str = Depends(require_project_role("developer"))):
    """過去のセーブポイントへロールバックする（F4、プロジェクトDeveloper以上）。

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
    """操作履歴（監査ログ）を新しい順で返す（管理者限定、F7）。"""
    return list_operations()


@router.post("/sync/check-all")
def post_sync_check_all(request: Request) -> dict[str, Any]:
    """Cloud Schedulerから定期実行され、全GASプロジェクトの変更有無をチェックする（F11）。

    人間のRBACとは別の認証経路（共有シークレットトークン）で保護する。
    """
    verify_scheduler_token(request)
    result = check_all_projects()
    return {"ok": True, **result}
