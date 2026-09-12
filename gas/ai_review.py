"""セーブ時のAI影響レビュー生成。

人間によるレビュー・承認ゲートを廃止する代わりに、セーブポイント作成時点で
前回セーブポイントからの累積差分をGemini APIに渡し、影響範囲・リスク・
確認しておくべき点を日本語でまとめる。あくまで参考情報であり、生成に失敗
しても（APIキー未設定・API障害等）セーブ自体は止めない（呼び出し側で
ベストエフォート運用する）。
"""
from __future__ import annotations

import difflib
from typing import Any

from google import genai

from auth.secrets import get_secret

GEMINI_API_KEY_SECRET_ID = "savepoint-gemini-api-key"
GEMINI_MODEL = "gemini-2.0-flash"


def generate_change_review(
    project_name: str,
    previous_files: list[dict[str, Any]],
    current_files: list[dict[str, Any]],
) -> str:
    """前回セーブポイントとの差分についてAIレビューコメントを生成する。"""
    api_key = get_secret(GEMINI_API_KEY_SECRET_ID)
    client = genai.Client(api_key=api_key)
    prompt = _build_prompt(project_name, previous_files, current_files)
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return response.text


def _build_prompt(
    project_name: str,
    previous_files: list[dict[str, Any]],
    current_files: list[dict[str, Any]],
) -> str:
    """差分レビュー用プロンプトを組み立てる。"""
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

    return "\n\n".join(
        [
            "次のGoogle Apps Scriptプロジェクトについて、前回の正式な記録（セーブポイント）からの変更内容をレビューしてください。",
            "非エンジニアの担当者が読む想定で、次の3点を日本語で簡潔にまとめてください（Markdown形式）:",
            "1. 何がどう変わったか（機能・処理の観点で平易に）",
            "2. 想定される影響・リスク（動作が変わる可能性がある箇所、外部連携やデータ保存に関わる変更等）",
            "3. 保存する前に確認しておくとよい点（あれば）",
            "差分が無いファイルは無視してください。過度に専門的な表現は避けてください。",
            f"# プロジェクト名\n{project_name}",
            "# 変更差分",
            "\n\n".join(diff_blocks) if diff_blocks else "（差分なし）",
        ]
    )
