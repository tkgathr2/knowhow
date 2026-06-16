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
# シグナル抽出も経営判断に直結＝質重視で上位モデル
SIGNAL_MODEL = "gpt-4o"

_SIGNAL_SYSTEM_PROMPT = (
    "あなたは社長専属の参謀です。会議・会話の文字起こしから、"
    "『社長が知るべき/判断すべきこと』だけを抜き出します。雑談・確定済みの報告・"
    "一般論・世間話は捨ててください。経営に効くものだけを残します。\n"
    "各シグナルを次の JSON オブジェクトで表し、JSON配列だけを出力してください（説明文・コードフェンス禁止）:\n"
    '{"type":"種別","title":"一言の見出し","detail":"1〜2文の背景","who":"誰が/誰について(不明はnull)","importance":1〜10}\n'
    "type は次のいずれか: "
    "decision(社長が決めるべき/判断待ち), risk(リスク・火種), opportunity(好機・商談・改善), "
    "promise(誰かが約束した宿題・TODO), complaint(人の不満・離職の芽), number(重要な数字・KPIの変化), other(その他)。\n"
    "importance は経営インパクトの大きさ。該当が無ければ空配列 [] を返す。最大15件。"
)

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


async def extract_signals(source_text: str) -> list[dict]:
    """1日分の録音テキストから経営判断シグナルを抽出（正規化済み行のリスト）。

    ベストエフォート：APIキー未設定・LLM障害・パース失敗のいずれでも例外を投げず []。
    返り値の各要素は koe_signal.parse_signals が正規化した dict。
    """
    from app import koe_signal

    if not settings.openai_api_key or not source_text.strip():
        return []
    try:
        resp = await _get_client().chat.completions.create(
            model=SIGNAL_MODEL,
            messages=[
                {"role": "system", "content": _SIGNAL_SYSTEM_PROMPT},
                {"role": "user", "content": source_text},
            ],
            temperature=0,
            max_tokens=1800,
        )
        return koe_signal.parse_signals(resp.choices[0].message.content, max_signals=15)
    except Exception:
        return []
