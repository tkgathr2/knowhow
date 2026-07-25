import asyncio
import math

import httpx
from openai import AsyncOpenAI

from app.config import settings

_openai_client: AsyncOpenAI | None = None

# Gemini embedContent の入力上限は 2,048 トークン。超過時は 400 が返るため
# 半分に切って再試行する（_embed_google の 400 ハンドリング参照）。
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_GEMINI_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BATCH_SIZE_LIMIT = 100  # batchEmbedContents の API 上限


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


def l2_normalize(vec: list[float]) -> list[float]:
    # gemini-embedding-001 は 3072 次元以外を正規化せずに返すため、
    # 保存前に必ず L2 正規化する（Google 公式ドキュメントの指示）。
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


async def _post_gemini(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response:
    """429/5xx を指数バックオフで再試行し、最終レスポンスを返す。"""
    resp: httpx.Response | None = None
    for attempt in range(_MAX_RETRIES + 1):
        resp = await client.post(
            url,
            json=payload,
            headers={"x-goog-api-key": settings.google_generative_ai_api_key},
        )
        if resp.status_code not in (429, 500, 502, 503):
            return resp
        if attempt < _MAX_RETRIES:
            await asyncio.sleep(2 ** attempt * 5)  # 5s, 10s, 20s
    return resp


async def _embed_google(text_value: str) -> list[float] | None:
    url = f"{_GEMINI_BASE}/{settings.embedding_model}:embedContent"
    text = text_value
    async with httpx.AsyncClient(timeout=_GEMINI_TIMEOUT) as client:
        for _ in range(3):  # 入力長超過(400)は半分に切って再試行
            payload = {
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": settings.embedding_dim,
                "taskType": "RETRIEVAL_DOCUMENT",
            }
            resp = await _post_gemini(client, url, payload)
            if resp.status_code == 200:
                values = resp.json().get("embedding", {}).get("values")
                if not values:
                    return None
                return l2_normalize(values)
            if resp.status_code == 400 and len(text) > 200:
                text = text[: len(text) // 2]
                continue
            return None
    return None


async def _embed_google_query(text_value: str) -> list[float] | None:
    url = f"{_GEMINI_BASE}/{settings.embedding_model}:embedContent"
    async with httpx.AsyncClient(timeout=_GEMINI_TIMEOUT) as client:
        payload = {
            "content": {"parts": [{"text": text_value[:4000]}]},
            "outputDimensionality": settings.embedding_dim,
            "taskType": "RETRIEVAL_QUERY",
        }
        resp = await _post_gemini(client, url, payload)
        if resp.status_code == 200:
            values = resp.json().get("embedding", {}).get("values")
            if not values:
                return None
            return l2_normalize(values)
        return None


async def create_embedding(text_value: str, *, task: str = "document") -> list[float] | None:
    """テキストを 1536 次元ベクトルにする。失敗時は None（呼び出し側契約を維持）。

    task: "document"（保存側・既定）| "query"（検索クエリ側）。
    Gemini では非対称タスク型（RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY）を使う。
    OpenAI 経路では task は無視される。
    """
    provider = settings.active_embedding_provider
    if provider == "google":
        if task == "query":
            return await _embed_google_query(text_value)
        return await _embed_google(text_value)
    if provider == "openai":
        client = _get_openai_client()
        resp = await client.embeddings.create(
            model=settings.embedding_model,
            input=text_value,
            dimensions=settings.embedding_dim,
        )
        return resp.data[0].embedding
    return None


async def create_embeddings_batch(texts: list[str]) -> list[list[float] | None]:
    """複数テキストを一括 embedding（reembed 用）。入力順と同順で返す。

    Google: batchEmbedContents（N 件 = 1 リクエスト）で無料枠の日次リクエスト上限を節約。
    バッチ全体が失敗したら 1 件ずつの単発呼び出しにフォールバックする
    （1 件の不正入力がバッチ全滅を招くのを防ぐ）。
    """
    provider = settings.active_embedding_provider
    if provider != "google":
        return [await create_embedding(t) for t in texts]

    results: list[list[float] | None] = []
    url = f"{_GEMINI_BASE}/{settings.embedding_model}:batchEmbedContents"
    model_ref = f"models/{settings.embedding_model}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        for start in range(0, len(texts), _BATCH_SIZE_LIMIT):
            batch = texts[start : start + _BATCH_SIZE_LIMIT]
            payload = {
                "requests": [
                    {
                        "model": model_ref,
                        "content": {"parts": [{"text": t if t else " "}]},
                        "outputDimensionality": settings.embedding_dim,
                        "taskType": "RETRIEVAL_DOCUMENT",
                    }
                    for t in batch
                ]
            }
            resp = await _post_gemini(client, url, payload)
            if resp.status_code == 200:
                embeddings = resp.json().get("embeddings", [])
                for i in range(len(batch)):
                    values = embeddings[i].get("values") if i < len(embeddings) else None
                    results.append(l2_normalize(values) if values else None)
            else:
                # バッチ失敗 → 単発フォールバック（長文 400 の切り詰め再試行も効く）
                for t in batch:
                    results.append(await _embed_google(t))
    return results


def escape_like(query: str) -> str:
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clamp_top_k(top_k: int, *, low: int = 1, high: int = 100) -> int:
    return max(low, min(top_k, high))
