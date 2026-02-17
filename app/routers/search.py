from fastapi import APIRouter, Depends, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import KbChunk, KbProject

router = APIRouter(tags=["search"])

_openai_client: AsyncOpenAI | None = None


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


async def _create_embedding(text_value: str) -> list[float] | None:
    if not settings.openai_api_key:
        return None

    client = _get_openai_client()
    resp = await client.embeddings.create(
        model=settings.embedding_model,
        input=text_value,
        dimensions=settings.embedding_dim,
    )
    return resp.data[0].embedding


class SearchRequest(BaseModel):
    project_key: str
    query: str
    top_k: int = 10
    threshold: float | None = None


class ChunkResult(BaseModel):
    chunk_id: int
    content: str
    chunk_type: str
    score: float
    tags: list[str]
    source_type: str
    source_id: int
    importance_score: int
    confidence_score: float


class SearchResponse(BaseModel):
    results: list[ChunkResult]
    query: str
    total: int


def _base_chunk_query(project_key: str, min_confidence: float) -> Select:
    return (
        select(
            KbChunk.id,
            KbChunk.content,
            KbChunk.chunk_type,
            KbChunk.tags,
            KbChunk.source_type,
            KbChunk.source_id,
            KbChunk.importance_score,
            KbChunk.confidence_score,
        )
        .where(
            KbChunk.project_key == project_key,
            KbChunk.is_deprecated.is_(False),
            KbChunk.confidence_score >= min_confidence,
        )
    )


@router.post("/search", response_model=SearchResponse)
async def search_chunks(req: SearchRequest, db: AsyncSession = Depends(get_db)) -> SearchResponse:
    project_row = await db.execute(select(KbProject).where(KbProject.project_key == req.project_key))
    project = project_row.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{req.project_key}' not found")

    min_confidence = req.threshold or project.search_confidence_threshold

    results_by_id: dict[int, ChunkResult] = {}

    query_embedding = None
    try:
        query_embedding = await _create_embedding(req.query)
    except Exception:
        query_embedding = None

    if query_embedding is not None:
        similarity = (1 - KbChunk.embedding.cosine_distance(query_embedding)).label("similarity")
        vector_q = (
            _base_chunk_query(req.project_key, min_confidence)
            .add_columns(similarity)
            .where(KbChunk.embedding.isnot(None))
            .order_by(similarity.desc())
            .limit(req.top_k)
        )

        vector_rows = await db.execute(vector_q)
        for row in vector_rows:
            results_by_id[row.id] = ChunkResult(
                chunk_id=row.id,
                content=row.content,
                chunk_type=row.chunk_type,
                score=float(row.similarity),
                tags=row.tags or [],
                source_type=row.source_type,
                source_id=row.source_id,
                importance_score=row.importance_score,
                confidence_score=row.confidence_score,
            )

    fts_q = (
        _base_chunk_query(req.project_key, min_confidence)
        .where(
            KbChunk.search_vector.isnot(None),
            KbChunk.search_vector.op("@@")(text("plainto_tsquery('simple', :q)")),
        )
        .params(q=req.query)
        .order_by(KbChunk.confidence_score.desc(), KbChunk.importance_score.desc())
        .limit(req.top_k)
    )

    fts_rows = await db.execute(fts_q)
    for row in fts_rows:
        if row.id in results_by_id:
            continue
        results_by_id[row.id] = ChunkResult(
            chunk_id=row.id,
            content=row.content,
            chunk_type=row.chunk_type,
            score=float(row.confidence_score),
            tags=row.tags or [],
            source_type=row.source_type,
            source_id=row.source_id,
            importance_score=row.importance_score,
            confidence_score=row.confidence_score,
        )

    if not results_by_id:
        like_q = (
            _base_chunk_query(req.project_key, min_confidence)
            .where(KbChunk.content.ilike(f"%{req.query}%"))
            .order_by(KbChunk.confidence_score.desc(), KbChunk.importance_score.desc())
            .limit(req.top_k)
        )
        like_rows = await db.execute(like_q)
        for row in like_rows:
            results_by_id[row.id] = ChunkResult(
                chunk_id=row.id,
                content=row.content,
                chunk_type=row.chunk_type,
                score=float(row.confidence_score) * 0.8,
                tags=row.tags or [],
                source_type=row.source_type,
                source_id=row.source_id,
                importance_score=row.importance_score,
                confidence_score=row.confidence_score,
            )

    results = sorted(results_by_id.values(), key=lambda r: r.score, reverse=True)[: req.top_k]
    return SearchResponse(results=results, query=req.query, total=len(results))
