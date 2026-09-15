"""AI README生成ロジック。

Vertex AI経由でGeminiを呼び出す。このアプリは既にGCP（gas-savepointプロジェクト）
上で動作しており、Cloud Run/ローカルのApplication Default Credentialsで認証
できるため、Google AI Studio発行の別建てAPIキーは不要（会長指摘、2026-09-13）。
"""
from __future__ import annotations

import json

from .vertex_client import GEMINI_MODEL, get_genai_client

# 1回のプロンプトに載せるソースコードの上限（文字数）。GASプロジェクトが極端に大きい場合に
# モデルの入力上限超過・レスポンス遅延・コスト増を招くため、ここで切り詰める（2026-09-15）。
MAX_SOURCE_CHARS = 120_000

# 台帳の「用途」欄に入れる一文の最大文字数（列幅に収まる程度に抑える）。
MAX_SUMMARY_CHARS = 60

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "台帳の一覧に表示する用途の一文（40文字程度、体言止め、記号や装飾なし）",
        },
        "readme": {
            "type": "string",
            "description": "日本語Markdown形式のREADME本文",
        },
    },
    "required": ["summary", "readme"],
}


def generate_readme(project_name: str, source_files: list[dict]) -> dict[str, str]:
    """GASソースから、台帳用の用途一文とREADME本文をまとめて生成する。

    戻り値: {"summary": 用途の一文, "readme": README本文}

    2026-09-15: 台帳の「用途」を手入力させていたが、READMEを生成する時点でAIは
    そのGASが何をするものかを把握しているため、同じ1回の生成で用途の一文も
    返させるようにした（生成回数は増えないのでコストも増えない）。
    """
    client = get_genai_client()
    prompt = _build_prompt(project_name, source_files)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config={"response_mime_type": "application/json", "response_schema": RESPONSE_SCHEMA},
    )
    text = response.text
    if not text or not text.strip():
        # 安全性フィルタや出力上限でテキストが返らないことがある。空のREADMEをそのまま
        # 保存すると「生成日時は入っているのに中身が無い」状態になるため、呼び出し側の
        # ベストエフォート処理で失敗として扱えるよう例外にする（2026-09-15）。
        raise RuntimeError("AIからREADME本文が返りませんでした")

    data = json.loads(text)
    readme = (data.get("readme") or "").strip()
    if not readme:
        raise RuntimeError("AIからREADME本文が返りませんでした")

    summary = " ".join((data.get("summary") or "").split())
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS] + "…"
    return {"summary": summary, "readme": readme}


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
            "次のGoogle Apps Scriptプロジェクトについて、以下の2つを日本語で作成してください。",
            "いずれも非エンジニアが読む想定で、専門用語は必要最小限にしてください。",
            "1. summary: 管理台帳の一覧に表示する「用途」の一文。"
            "そのGASが何のためのものかが一目で分かるように40文字程度でまとめる。"
            "体言止めにし、「このGASは」等の前置き・記号・装飾は付けない"
            "（例: 顧問先へのLINE月次一斉送信、請求書PDFのドライブ自動振り分け）。",
            "2. readme: 用途・主な処理内容・使い方をMarkdown形式で簡潔にまとめたREADME本文。",
            f"# プロジェクト名\n{project_name}",
            "# ソースファイル",
            "\n\n".join(file_blocks),
        ]
    )
