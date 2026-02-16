import hashlib

from fastapi import APIRouter, Depends
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import KbChunk, KbProject, KbSession

router = APIRouter(tags=["devin"])

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


class RecallRequest(BaseModel):
    project_key: str
    query: str
    top_k: int = 5


class RecallChunk(BaseModel):
    chunk_id: int
    content: str
    chunk_type: str
    score: float
    tags: list[str]


class RecallResponse(BaseModel):
    results: list[RecallChunk]
    query: str
    total: int
    project_key: str


@router.post("/devin/recall", response_model=RecallResponse)
async def recall(
    req: RecallRequest, db: AsyncSession = Depends(get_db)
) -> RecallResponse:
    project_row = await db.execute(
        select(KbProject).where(KbProject.project_key == req.project_key)
    )
    project = project_row.scalar_one_or_none()
    if not project:
        return RecallResponse(
            results=[], query=req.query, total=0, project_key=req.project_key
        )

    min_confidence = project.search_confidence_threshold
    results_by_id: dict[int, RecallChunk] = {}

    query_embedding = None
    try:
        query_embedding = await _create_embedding(req.query)
    except Exception:
        query_embedding = None

    base_where = [
        KbChunk.project_key == req.project_key,
        KbChunk.is_deprecated.is_(False),
        KbChunk.confidence_score >= min_confidence,
    ]

    if query_embedding is not None:
        similarity = (
            1 - KbChunk.embedding.cosine_distance(query_embedding)
        ).label("similarity")
        vector_q = (
            select(
                KbChunk.id,
                KbChunk.content,
                KbChunk.chunk_type,
                KbChunk.tags,
                similarity,
            )
            .where(*base_where, KbChunk.embedding.isnot(None))
            .order_by(similarity.desc())
            .limit(req.top_k)
        )

        vector_rows = await db.execute(vector_q)
        for row in vector_rows:
            results_by_id[row.id] = RecallChunk(
                chunk_id=row.id,
                content=row.content,
                chunk_type=row.chunk_type,
                score=float(row.similarity),
                tags=row.tags or [],
            )

    fts_q = (
        select(
            KbChunk.id,
            KbChunk.content,
            KbChunk.chunk_type,
            KbChunk.tags,
            KbChunk.confidence_score,
        )
        .where(
            *base_where,
            KbChunk.search_vector.isnot(None),
            KbChunk.search_vector.op("@@")(
                text("plainto_tsquery('simple', :q)")
            ),
        )
        .params(q=req.query)
        .order_by(KbChunk.confidence_score.desc())
        .limit(req.top_k)
    )

    fts_rows = await db.execute(fts_q)
    for row in fts_rows:
        if row.id in results_by_id:
            continue
        results_by_id[row.id] = RecallChunk(
            chunk_id=row.id,
            content=row.content,
            chunk_type=row.chunk_type,
            score=float(row.confidence_score),
            tags=row.tags or [],
        )

    results = sorted(
        results_by_id.values(), key=lambda r: r.score, reverse=True
    )[: req.top_k]
    return RecallResponse(
        results=results,
        query=req.query,
        total=len(results),
        project_key=req.project_key,
    )


class MemorizeRequest(BaseModel):
    project_key: str
    tool: str = "devin"
    status: str = "success"
    environment: str = "local"
    raw_log: str
    tags: list[str] = []


class MemorizeResponse(BaseModel):
    session_id: int
    chunk_id: int | None
    message: str


@router.post("/devin/memorize", response_model=MemorizeResponse)
async def memorize(
    req: MemorizeRequest, db: AsyncSession = Depends(get_db)
) -> MemorizeResponse:
    project_row = await db.execute(
        select(KbProject).where(KbProject.project_key == req.project_key)
    )
    project = project_row.scalar_one_or_none()
    if not project:
        project = KbProject(
            project_key=req.project_key,
            display_name=req.project_key,
        )
        db.add(project)
        await db.flush()

    log_hash = hashlib.sha256(req.raw_log.encode("utf-8")).hexdigest()

    existing = await db.execute(
        select(KbSession).where(
            KbSession.project_key == req.project_key,
            KbSession.hash == log_hash,
        )
    )
    if existing.scalar_one_or_none():
        return MemorizeResponse(
            session_id=0, chunk_id=None, message="Already memorized"
        )

    session = KbSession(
        project_key=req.project_key,
        tool=req.tool,
        status=req.status,
        environment=req.environment,
        raw_log=req.raw_log,
        normalized_log=req.raw_log.strip(),
        tags=req.tags,
        hash=log_hash,
        ingest_state="summarized",
    )
    db.add(session)
    await db.flush()

    chunk = KbChunk(
        project_key=req.project_key,
        source_type="session",
        source_id=session.id,
        chunk_type="session_log",
        content=session.normalized_log,
        importance_score=5,
        confidence_score=0.9,
        alpha=9.0,
        beta=1.0,
        tags=req.tags,
        meta={
            "tool": req.tool,
            "status": req.status,
            "environment": req.environment,
        },
    )
    db.add(chunk)

    try:
        embedding = await _create_embedding(session.normalized_log)
        if embedding is not None:
            chunk.embedding = embedding
            chunk.embedding_model = settings.embedding_model
            chunk.embedding_dimensions = settings.embedding_dim
            session.ingest_state = "embedded"
    except Exception:
        session.ingest_state = "failed_embedding"

    await db.commit()
    await db.refresh(session)
    await db.refresh(chunk)

    return MemorizeResponse(
        session_id=session.id,
        chunk_id=chunk.id,
        message="Memorized",
    )
