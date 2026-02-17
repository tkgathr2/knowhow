from openai import AsyncOpenAI

from app.config import settings

_openai_client: AsyncOpenAI | None = None


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


async def create_embedding(text_value: str) -> list[float] | None:
    if not settings.openai_api_key:
        return None
    client = _get_openai_client()
    resp = await client.embeddings.create(
        model=settings.embedding_model,
        input=text_value,
        dimensions=settings.embedding_dim,
    )
    return resp.data[0].embedding


def escape_like(query: str) -> str:
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clamp_top_k(top_k: int, *, low: int = 1, high: int = 100) -> int:
    return max(low, min(top_k, high))
