"""AI README生成ロジック。

Vertex AI経由でGeminiを呼び出す。このアプリは既にGCP（gas-savepointプロジェクト）
上で動作しており、Cloud Run/ローカルのApplication Default Credentialsで認証
できるため、Google AI Studio発行の別建てAPIキーは不要（会長指摘、2026-09-13）。
"""
from __future__ import annotations

from .vertex_client import GEMINI_MODEL, get_genai_client

# 1回のプロンプトに載せるソースコードの上限（文字数）。GASプロジェクトが極端に大きい場合に
# モデルの入力上限超過・レスポンス遅延・コスト増を招くため、ここで切り詰める（2026-09-15）。
MAX_SOURCE_CHARS = 120_000


def generate_readme(project_name: str, source_files: list[dict]) -> str:
    """GASソースから日本語READMEを生成する。"""
    client = get_genai_client()
    prompt = _build_prompt(project_name, source_files)
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    text = response.text
    if not text or not text.strip():
        # 安全性フィルタや出力上限でテキストが返らないことがある。空のREADMEをそのまま
        # 保存すると「生成日時は入っているのに中身が無い」状態になるため、呼び出し側の
        # ベストエフォート処理で失敗として扱えるよう例外にする（2026-09-15）。
        raise RuntimeError("AIからREADME本文が返りませんでした")
    return text


def _build_prompt(project_name: str, source_files: list[dict]) -> str:
    """README生成用プロンプトを組み立てる（合計がMAX_SOURCE_CHARSを超えた分は切り詰める）。"""
    file_blocks = []
    remaining = MAX_SOURCE_CHARS
    for index, source_file in enumerate(source_files, start=1):
        source = str(source_file.get("source", ""))
        if remaining <= 0:
            file_blocks.append(
                f"## ファイル {index}\nname: {source_file.get('name', '')}\n"
                "（サイズ上限のため本文は省略しています）"
            )
            continue
        if len(source) > remaining:
            source = source[:remaining] + "\n…（以下、サイズ上限のため省略）"
        remaining -= len(source)
        file_blocks.append(
            "\n".join(
                [
                    f"## ファイル {index}",
                    f"name: {source_file.get('name', '')}",
                    f"type: {source_file.get('type', '')}",
                    "source:",
                    "```",
                    source,
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
