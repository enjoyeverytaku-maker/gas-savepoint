"""AI README生成ロジック。

Vertex AI経由でGeminiを呼び出す。このアプリは既にGCP（gas-savepointプロジェクト）
上で動作しており、Cloud Run/ローカルのApplication Default Credentialsで認証
できるため、Google AI Studio発行の別建てAPIキーは不要（会長指摘、2026-09-13）。
"""
from __future__ import annotations

from .vertex_client import GEMINI_MODEL, get_genai_client


def generate_readme(project_name: str, source_files: list[dict]) -> str:
    """GASソースから日本語READMEを生成する。"""
    client = get_genai_client()
    prompt = _build_prompt(project_name, source_files)
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return response.text


def _build_prompt(project_name: str, source_files: list[dict]) -> str:
    """README生成用プロンプトを組み立てる。"""
    file_blocks = []
    for index, source_file in enumerate(source_files, start=1):
        file_blocks.append(
            "\n".join(
                [
                    f"## ファイル {index}",
                    f"name: {source_file.get('name', '')}",
                    f"type: {source_file.get('type', '')}",
                    "source:",
                    "```",
                    str(source_file.get("source", "")),
                    "```",
                ]
            )
        )

    return "\n\n".join(
        [
            "次のGoogle Apps Scriptプロジェクトについて、READMEを作成してください。",
            "このGoogle Apps Scriptプロジェクトの用途・主な処理内容・使い方を日本語のMarkdown形式で簡潔にまとめてください。",
            "非エンジニアが読む想定で、専門用語は必要最小限にしてください。",
            f"# プロジェクト名\n{project_name}",
            "# ソースファイル",
            "\n\n".join(file_blocks),
        ]
    )
