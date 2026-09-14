"""ワンクリックロールバック（F4、spec.md §5・§12）。

Codexセカンドオピニオン（docs/codex_review_2026-09-05.md）で指摘された、
このシステムで最も事故が起きやすい箇所。以下2点を必ず守ること。

1. 復元前の完全バックアップ: Apps Script APIのupdateContentは差分パッチではなく
   全体置換のため、復元を実行する直前に「今まさにGAS本体にある状態」を
   自動でセーブポイントとして保存する。これによりロールバック操作自体も
   取り消せる（同じ仕組みで、その自動バックアップへ再度ロールバックすればよい）。
2. 楽観ロック: クライアントが把握している「現在の状態のハッシュ」
   (expected_current_hash) と、今まさにAPIから取得した実際のハッシュを比較する。
   一致しなければ、ユーザーが画面を見てから復元ボタンを押すまでの間に
   誰か（他の担当者や本人の別タブ操作）がGASを直接編集した可能性があるため、
   コンフリクトとして拒否する。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from auth.oauth import get_credentials_for

from .apps_script import AppsScriptAPIError, fetch_source_files, update_content
from .audit import ACTION_ROLLBACK, log_operation
from .diff import calculate_source_hash
from .projects import get_project
from .savepoints import create_savepoint, get_savepoint_by_version


@dataclass
class RollbackConflictError(Exception):
    """楽観ロック違反（現在のGAS状態がクライアントの想定と一致しない）。"""

    current_hash: str
    expected_hash: str


@dataclass
class RollbackTargetNotFoundError(Exception):
    """指定されたプロジェクトまたはバージョンが存在しない。"""

    message: str


def rollback_to_version(
    project_id: str,
    target_version_no: int,
    performed_by: str,
    expected_current_hash: str | None = None,
) -> dict[str, Any]:
    """指定バージョンへロールバックする。

    戻り値: 復元先バージョン番号と、復元前に自動作成されたバックアップの
    バージョン番号（このバックアップへ再度ロールバックすれば取り消せる）。
    """
    project = get_project(project_id)
    if project is None:
        raise RollbackTargetNotFoundError(f"GASプロジェクトが見つかりません: {project_id}")

    target_savepoint = get_savepoint_by_version(project_id, target_version_no)
    if target_savepoint is None:
        raise RollbackTargetNotFoundError(
            f"復元先のセーブポイントが見つかりません: project_id={project_id}, version_no={target_version_no}"
        )

    creds = get_credentials_for(project["google_account"])

    try:
        current_files = fetch_source_files(project["script_id"], creds)
    except AppsScriptAPIError:
        log_operation(
            action=ACTION_ROLLBACK,
            project_id=project_id,
            user=performed_by,
            target_version=target_version_no,
            result="failed_fetch_current",
        )
        raise

    current_hash = calculate_source_hash(current_files)

    # 楽観ロック: クライアントが把握している状態と実際の現在状態が食い違っていないか確認する
    if expected_current_hash is not None and expected_current_hash != current_hash:
        log_operation(
            action=ACTION_ROLLBACK,
            project_id=project_id,
            user=performed_by,
            target_version=target_version_no,
            result="conflict",
            details={"current_hash": current_hash, "expected_hash": expected_current_hash},
        )
        raise RollbackConflictError(current_hash=current_hash, expected_hash=expected_current_hash)

    # 1. 復元前に、今まさにGAS本体にある状態を自動バックアップする（ロールバック自体を取り消せるようにする）
    backup_savepoint = create_savepoint(
        project_id=project_id,
        source_files=current_files,
        comment=f"ロールバック前の自動バックアップ（v{target_version_no}への復元直前）",
        created_by=performed_by,
    )

    # 2. 過去のソースをGAS本体へ反映する（全体置換）
    try:
        update_content(project["script_id"], target_savepoint["source_files"], creds)
    except AppsScriptAPIError:
        log_operation(
            action=ACTION_ROLLBACK,
            project_id=project_id,
            user=performed_by,
            target_version=target_version_no,
            result="failed_update",
            details={"auto_backup_version_no": backup_savepoint["version_no"]},
        )
        raise

    log_operation(
        action=ACTION_ROLLBACK,
        project_id=project_id,
        user=performed_by,
        target_version=target_version_no,
        result="success",
        details={"auto_backup_version_no": backup_savepoint["version_no"]},
    )

    return {
        "restored_version_no": target_version_no,
        "auto_backup_version_no": backup_savepoint["version_no"],
    }
