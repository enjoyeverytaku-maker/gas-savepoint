"""GASソース差分計算。"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
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
    include_lines: bool = True,
) -> list[dict[str, Any]]:
    """直前セーブポイントとの差分をファイル単位で計算する。

    include_lines=Falseにすると行単位の差分（lines）を省き、ファイル単位の集計だけを返す。
    変更検知の記録（gas/changes.py）は画面側でファイル名と増減行数しか使わないうえ、
    linesを持たせるとFirestoreの1ドキュメント上限（1MiB）をソース本文と二重に圧迫するため、
    保存用途ではFalseを使う（2026-09-15）。
    """
    previous_by_key = _index_files(previous_files or [])
    current_by_key = _index_files(current_files)

    results: list[dict[str, Any]] = []
    for key in sorted(current_by_key.keys() - previous_by_key.keys()):
        current = current_by_key[key]
        added = len(_split_lines(current.get("source", "")))
        results.append(_diff_result(current, "Added", added, 0, "", current.get("source", ""), include_lines))

    for key in sorted(previous_by_key.keys() & current_by_key.keys()):
        previous = previous_by_key[key]
        current = current_by_key[key]
        if previous.get("source", "") == current.get("source", ""):
            continue
        results.append(
            _diff_result(
                current,
                "Modified",
                None,
                None,
                previous.get("source", ""),
                current.get("source", ""),
                include_lines,
            )
        )

    for key in sorted(previous_by_key.keys() - current_by_key.keys()):
        previous = previous_by_key[key]
        deleted = len(_split_lines(previous.get("source", "")))
        results.append(_diff_result(previous, "Deleted", 0, deleted, previous.get("source", ""), "", include_lines))

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
    """追加行数と削除行数を集計する。

    2026-09-15変更: 以前はdifflib.ndiffを使っていたが、ndiffは行内の類似度まで見る分
    計算量が大きく（最悪O(n^2)）、しかも画面表示用のunified_diffと二重に差分計算していた。
    表示用と同じSequenceMatcherの操作列から数えることで、計算を1回に減らしつつ
    表示される差分と集計値が必ず一致するようにした。
    """
    matcher = difflib.SequenceMatcher(None, _split_lines(previous_source), _split_lines(current_source))
    added = 0
    deleted = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            deleted += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, deleted


def _count_from_lines(lines: list[dict[str, Any]]) -> tuple[int, int]:
    """組み立て済みの行差分から追加・削除行数を数える（同じ差分を2回計算しないため）。"""
    added = sum(1 for line in lines if line["type"] == "insert")
    deleted = sum(1 for line in lines if line["type"] == "delete")
    return added, deleted


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_lines(previous_source: str, current_source: str, context: int = 3) -> list[dict[str, Any]]:
    """git風のハント形式で行単位の差分を組み立てる（画面表示用）。"""
    previous_lines = _split_lines(previous_source)
    current_lines = _split_lines(current_source)
    unified = list(
        difflib.unified_diff(previous_lines, current_lines, n=context, lineterm="")
    )[2:]  # 先頭2行（---/+++ヘッダー）は画面表示に不要なので除く

    result: list[dict[str, Any]] = []
    a_line = b_line = 0
    for line in unified:
        if line.startswith("@@"):
            match = _HUNK_HEADER_RE.match(line)
            if match:
                a_line = int(match.group(1))
                b_line = int(match.group(2))
            result.append({"type": "header", "a": None, "b": None, "text": line})
            continue
        if line.startswith("+"):
            result.append({"type": "insert", "a": None, "b": b_line, "text": line[1:]})
            b_line += 1
        elif line.startswith("-"):
            result.append({"type": "delete", "a": a_line, "b": None, "text": line[1:]})
            a_line += 1
        else:
            result.append({"type": "context", "a": a_line, "b": b_line, "text": line[1:] if line.startswith(" ") else line})
            a_line += 1
            b_line += 1
    return result


def _diff_result(
    file: dict[str, Any],
    status: str,
    added: int | None,
    deleted: int | None,
    previous_source: str,
    current_source: str,
    include_lines: bool = True,
) -> dict[str, Any]:
    """差分レスポンスの1件を組み立てる。

    added/deletedにNoneを渡すと、行差分（またはSequenceMatcher）から自動集計する。
    """
    lines = _diff_lines(previous_source, current_source) if include_lines else []
    if added is None or deleted is None:
        added, deleted = _count_from_lines(lines) if include_lines else _count_changed_lines(previous_source, current_source)
    return {
        "name": file.get("name", ""),
        "type": file.get("type", ""),
        "status": status,
        "added_lines": added,
        "deleted_lines": deleted,
        "lines": lines,
    }
