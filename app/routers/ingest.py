import hashlib
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import KbChunk, KbProject, KbSession

router = APIRouter(tags=["ingest"])

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


class IngestRequest(BaseModel):
    project_key: str
    tool: str  # devin, claude_code, cursor
    status: str = "success"  # success, fail, partial
    environment: str = "local"  # local, prod
    raw_log: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    tags: list[str] = []


class IngestResponse(BaseModel):
    session_id: int
    ingest_state: str
    message: str


@router.post("/ingest", response_model=IngestResponse)
async def ingest_session(req: IngestRequest, db: AsyncSession = Depends(get_db)) -> IngestResponse:
    project_row = await db.execute(select(KbProject).where(KbProject.project_key == req.project_key))
    project = project_row.scalar_one_or_none()
    if not project:
        project = KbProject(project_key=req.project_key, display_name=req.project_key)
        db.add(project)
        await db.flush()

    log_hash = hashlib.sha256(req.raw_log.encode("utf-8")).hexdigest()

    existing = await db.execute(
        select(KbSession).where(KbSession.project_key == req.project_key, KbSession.hash == log_hash)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Duplicate session log")

    duration = None
    if req.started_at and req.ended_at:
        duration = int((req.ended_at - req.started_at).total_seconds())

    session = KbSession(
        project_key=req.project_key,
        tool=req.tool,
        status=req.status,
        environment=req.environment,
        started_at=req.started_at,
        ended_at=req.ended_at,
        duration_seconds=duration,
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
        tags=req.tags,
        meta={"tool": req.tool, "status": req.status, "environment": req.environment},
    )
    db.add(chunk)

    message = "Session ingested"
    try:
        embedding = await _create_embedding(session.normalized_log)
        if embedding is not None:
            chunk.embedding = embedding
            chunk.embedding_model = settings.embedding_model
            chunk.embedding_dimensions = settings.embedding_dim
            session.ingest_state = "embedded"
    except Exception:
        session.ingest_state = "failed_embedding"
        message = "Session ingested (embedding failed)"

    await db.commit()
    await db.refresh(session)

    return IngestResponse(session_id=session.id, ingest_state=session.ingest_state, message=message)
