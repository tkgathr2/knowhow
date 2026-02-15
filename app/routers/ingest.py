import hashlib
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import KbProject, KbSession

router = APIRouter(tags=["ingest"])


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
    project = await db.execute(select(KbProject).where(KbProject.project_key == req.project_key))
    if not project.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"Project '{req.project_key}' not found")

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
        ingest_state="queued",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    return IngestResponse(
        session_id=session.id,
        ingest_state=session.ingest_state,
        message="Session queued for processing",
    )
