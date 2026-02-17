from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.embedding import clamp_top_k, create_embedding, escape_like
from app.models import KbChunk, KbProject

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    project_key: str
    query: str
    top_k: int = Field(default=10, ge=1, le=100)
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

    top_k = clamp_top_k(req.top_k)

    query_embedding = None
    try:
        query_embedding = await create_embedding(req.query)
    except Exception:
        query_embedding = None

    if query_embedding is not None:
        similarity = (1 - KbChunk.embedding.cosine_distance(query_embedding)).label("similarity")
        vector_q = (
            _base_chunk_query(req.project_key, min_confidence)
            .add_columns(similarity)
            .where(KbChunk.embedding.isnot(None))
            .order_by(similarity.desc())
            .limit(top_k)
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
        .limit(top_k)
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
        escaped = escape_like(req.query)
        like_q = (
            _base_chunk_query(req.project_key, min_confidence)
            .where(KbChunk.content.ilike(f"%{escaped}%"))
            .order_by(KbChunk.confidence_score.desc(), KbChunk.importance_score.desc())
            .limit(top_k)
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

    results = sorted(results_by_id.values(), key=lambda r: r.score, reverse=True)[:top_k]
    return SearchResponse(results=results, query=req.query, total=len(results))
