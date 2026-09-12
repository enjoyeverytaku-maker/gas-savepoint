"""セーブ時のAI影響レビュー生成。

人間によるレビュー・承認ゲートを廃止する代わりに、セーブポイント作成時点で
前回セーブポイントからの累積差分をGemini API（Vertex AI経由、ADC認証。
gas/vertex_client.py参照）に渡し、影響度・要約・観点別の確認事項を構造化
データとしてまとめる。あくまで参考情報であり、生成に失敗しても（API障害等）
セーブ自体は止めない（呼び出し側でベストエフォート運用する）。
"""
from __future__ import annotations

import difflib
import json
from typing import Any

from .vertex_client import GEMINI_MODEL, get_genai_client

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "risk_level": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "想定される影響の大きさ",
        },
        "summary": {"type": "string", "description": "一文でのまとめ"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "items"],
            },
        },
    },
    "required": ["risk_level", "summary", "sections"],
}


def generate_change_review(
    project_name: str,
    previous_files: list[dict[str, Any]],
    current_files: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """前回セーブポイントとの差分についてAIレビューを生成する。

    差分が無い場合はNoneを返す。
    """
    diff_blocks = _build_diff_blocks(previous_files, current_files)
    if not diff_blocks:
        return None

    client = get_genai_client()
    prompt = _build_prompt(project_name, diff_blocks)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config={"response_mime_type": "application/json", "response_schema": RESPONSE_SCHEMA},
    )
    return json.loads(response.text)


def _build_diff_blocks(
    previous_files: list[dict[str, Any]],
    current_files: list[dict[str, Any]],
) -> list[str]:
    """変更のあったファイルごとにunified diffのブロックを作る。"""
    previous_by_key = {(f.get("name", ""), f.get("type", "")): f for f in previous_files}
    current_by_key = {(f.get("name", ""), f.get("type", "")): f for f in current_files}

    diff_blocks: list[str] = []
    for key in sorted(current_by_key.keys() | previous_by_key.keys()):
        previous_source = previous_by_key.get(key, {}).get("source", "")
        current_source = current_by_key.get(key, {}).get("source", "")
        if previous_source == current_source:
            continue
        name, file_type = key
        unified = "\n".join(
            difflib.unified_diff(
                previous_source.splitlines(),
                current_source.splitlines(),
                fromfile=f"{name}（変更前）",
                tofile=f"{name}（変更後）",
                lineterm="",
            )
        )
        diff_blocks.append(f"## ファイル: {name} ({file_type})\n```diff\n{unified}\n```")
    return diff_blocks


def _build_prompt(project_name: str, diff_blocks: list[str]) -> str:
    """差分レビュー用プロンプトを組み立てる。"""
    return "\n\n".join(
        [
            "次のGoogle Apps Scriptプロジェクトについて、前回の正式な記録（セーブポイント）からの変更内容をレビューしてください。",
            "非エンジニアの担当者が読む想定で、次を日本語でまとめてください:",
            "- risk_level: 想定される影響の大きさ（low/medium/high）。外部連携・データ保存・金額計算等に関わる変更はhighより寄りに判定する",
            "- summary: 変更内容を一文で要約",
            "- sections: 以下3つの観点でまとめる。各itemsは平易な日本語の箇条書き（専門用語は避ける）",
            "  1. 「何が変わったか」（機能・処理の観点で）",
            "  2. 「想定される影響・リスク」（動作が変わる可能性がある箇所、外部連携やデータ保存に関わる変更等）",
            "  3. 「保存する前に確認しておくとよい点」（無ければ空配列でよい）",
            f"# プロジェクト名\n{project_name}",
            "# 変更差分",
            "\n\n".join(diff_blocks),
        ]
    )
