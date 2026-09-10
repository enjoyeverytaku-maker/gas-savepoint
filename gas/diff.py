"""GASソース差分計算。"""
from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any


def calculate_source_hash(source_files: list[dict[str, Any]]) -> str:
    """ソースファイル一覧から安定したハッシュ値を計算する。"""
    normalized = sorted(
        (
            {
                "name": file.get("name", ""),
                "type": file.get("type", ""),
                "source": file.get("source", ""),
            }
            for file in source_files
        ),
        key=lambda file: (file["name"], file["type"]),
    )
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def calculate_diff(
    current_files: list[dict[str, Any]],
    previous_files: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """直前セーブポイントとの差分をファイル単位で計算する。"""
    previous_by_key = _index_files(previous_files or [])
    current_by_key = _index_files(current_files)

    results: list[dict[str, Any]] = []
    for key in sorted(current_by_key.keys() - previous_by_key.keys()):
        current = current_by_key[key]
        added = len(_split_lines(current.get("source", "")))
        results.append(_diff_result(current, "Added", added, 0))

    for key in sorted(previous_by_key.keys() & current_by_key.keys()):
        previous = previous_by_key[key]
        current = current_by_key[key]
        if previous.get("source", "") == current.get("source", ""):
            continue
        added, deleted = _count_changed_lines(previous.get("source", ""), current.get("source", ""))
        results.append(_diff_result(current, "Modified", added, deleted))

    for key in sorted(previous_by_key.keys() - current_by_key.keys()):
        previous = previous_by_key[key]
        deleted = len(_split_lines(previous.get("source", "")))
        results.append(_diff_result(previous, "Deleted", 0, deleted))

    return results


def _index_files(source_files: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """ファイル名と種別でソースファイルを引ける形にする。"""
    return {
        (str(file.get("name", "")), str(file.get("type", ""))): file
        for file in source_files
    }


def _split_lines(source: str) -> list[str]:
    """ソース文字列を行単位に分割する。"""
    return source.splitlines()


def _count_changed_lines(previous_source: str, current_source: str) -> tuple[int, int]:
    """difflibで追加行数と削除行数を簡易集計する。"""
    added = 0
    deleted = 0
    for line in difflib.ndiff(_split_lines(previous_source), _split_lines(current_source)):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            deleted += 1
    return added, deleted


def _diff_result(file: dict[str, Any], status: str, added: int, deleted: int) -> dict[str, Any]:
    """差分レスポンスの1件を組み立てる。"""
    return {
        "name": file.get("name", ""),
        "type": file.get("type", ""),
        "status": status,
        "added_lines": added,
        "deleted_lines": deleted,
    }
