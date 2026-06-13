"""ロア（Lore・録音資産）チャンクの話題タグ付け。

gpt-4o-mini で会話チャンクに日本語の話題タグを 3〜6 個付ける。ベストエフォート：
APIキー未設定・LLM障害・パース失敗のいずれでも例外を投げず [] を返す（取込を止めない）。
※「ロア」はプロダクト名。内部実装の識別子は koe（モジュール名）を踏襲している。
"""

from __future__ import annotations

from openai import AsyncOpenAI

from app import koe_chunk, koe_digest
from app.config import settings

TAG_MODEL = "gpt-4o-mini"
# ダイジェストは社長が読む成果物＝質重視で上位モデル
DIGEST_MODEL = "gpt-4o"

_SYSTEM_PROMPT = (
    "あなたは会議・会話の文字起こしに話題タグを付ける専門家です。"
    "入力された会話に対し、内容を表す日本語の短い話題タグを3〜6個、JSON配列だけで出力してください。"
    "例: [\"採用\", \"交通誘導\", \"資金繰り\"]。人名や固有名詞より、検索で役立つ話題・テーマを優先。"
    "説明文やコードフェンスは付けず、JSON配列のみ返すこと。"
)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def tag_chunk(content: str) -> list[str]:
    """会話チャンクから話題タグを抽出（失敗時 []）。"""
    if not settings.openai_api_key or not content.strip():
        return []
    try:
        resp = await _get_client().chat.completions.create(
            model=TAG_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content[:4000]},
            ],
            temperature=0,
            max_tokens=120,
        )
        return koe_chunk.parse_tags(resp.choices[0].message.content)
    except Exception:
        return []


async def generate_daily_digest(source_text: str) -> str | None:
    """1日分の録音テキストから経営ダイジェスト(Markdown)を生成。失敗時 None（呼び出し側がフォールバック）。"""
    if not settings.openai_api_key or not source_text.strip():
        return None
    try:
        resp = await _get_client().chat.completions.create(
            model=DIGEST_MODEL,
            messages=[
                {"role": "system", "content": koe_digest.DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": source_text},
            ],
            temperature=0.2,
            max_tokens=1500,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception:
        return None
