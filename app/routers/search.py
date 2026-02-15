from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import KbChunk, KbProject

router = APIRouter(tags=["search"])


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


@router.post("/search", response_model=SearchResponse)
async def search_chunks(req: SearchRequest, db: AsyncSession = Depends(get_db)) -> SearchResponse:
    project_row = await db.execute(select(KbProject).where(KbProject.project_key == req.project_key))
    project = project_row.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{req.project_key}' not found")

    min_confidence = req.threshold or project.search_confidence_threshold

    query = (
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
            KbChunk.project_key == req.project_key,
            KbChunk.is_deprecated.is_(False),
            KbChunk.search_vector.isnot(None),
        )
        .where(KbChunk.search_vector.op("@@")(text("plainto_tsquery('simple', :q)")))
        .where(KbChunk.confidence_score >= min_confidence)
        .params(q=req.query)
        .order_by(KbChunk.confidence_score.desc(), KbChunk.importance_score.desc())
        .limit(req.top_k)
    )

    rows = await db.execute(query)
    results = []
    for row in rows:
        results.append(
            ChunkResult(
                chunk_id=row.id,
                content=row.content,
                chunk_type=row.chunk_type,
                score=row.confidence_score,
                tags=row.tags or [],
                source_type=row.source_type,
                source_id=row.source_id,
                importance_score=row.importance_score,
                confidence_score=row.confidence_score,
            )
        )

    return SearchResponse(results=results, query=req.query, total=len(results))
