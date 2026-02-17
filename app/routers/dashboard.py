from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import KbChunk, KbProject, KbSession

router = APIRouter(tags=["dashboard"])


class ProjectStats(BaseModel):
    project_key: str
    display_name: str | None
    session_count: int
    chunk_count: int
    embedded_chunk_count: int
    latest_memorize_at: datetime | None


class StatsResponse(BaseModel):
    total_projects: int
    total_sessions: int
    total_chunks: int
    total_embedded: int
    projects: list[ProjectStats]


@router.get("/stats", response_model=StatsResponse)
async def get_stats(db: AsyncSession = Depends(get_db)) -> StatsResponse:
    projects_q = await db.execute(select(KbProject))
    projects = projects_q.scalars().all()

    project_stats: list[ProjectStats] = []
    total_sessions = 0
    total_chunks = 0
    total_embedded = 0

    for p in projects:
        sess_count_row = await db.execute(
            select(func.count(KbSession.id)).where(
                KbSession.project_key == p.project_key
            )
        )
        sess_count = sess_count_row.scalar() or 0

        chunk_count_row = await db.execute(
            select(func.count(KbChunk.id)).where(
                KbChunk.project_key == p.project_key
            )
        )
        chunk_count = chunk_count_row.scalar() or 0

        embedded_row = await db.execute(
            select(func.count(KbChunk.id)).where(
                KbChunk.project_key == p.project_key,
                KbChunk.embedding.isnot(None),
            )
        )
        embedded_count = embedded_row.scalar() or 0

        latest_row = await db.execute(
            select(func.max(KbSession.created_at)).where(
                KbSession.project_key == p.project_key
            )
        )
        latest_at = latest_row.scalar()

        project_stats.append(
            ProjectStats(
                project_key=p.project_key,
                display_name=p.display_name,
                session_count=sess_count,
                chunk_count=chunk_count,
                embedded_chunk_count=embedded_count,
                latest_memorize_at=latest_at,
            )
        )

        total_sessions += sess_count
        total_chunks += chunk_count
        total_embedded += embedded_count

    return StatsResponse(
        total_projects=len(projects),
        total_sessions=total_sessions,
        total_chunks=total_chunks,
        total_embedded=total_embedded,
        projects=project_stats,
    )


class RecentEntry(BaseModel):
    session_id: int
    project_key: str
    tool: str
    status: str
    environment: str
    tags: list[str]
    content_preview: str
    created_at: datetime
    has_embedding: bool


class RecentResponse(BaseModel):
    entries: list[RecentEntry]
    total: int


@router.get("/recent", response_model=RecentResponse)
async def get_recent(
    limit: int = 20,
    project_key: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RecentResponse:
    q = (
        select(
            KbSession.id,
            KbSession.project_key,
            KbSession.tool,
            KbSession.status,
            KbSession.environment,
            KbSession.tags,
            KbSession.normalized_log,
            KbSession.created_at,
            KbChunk.embedding.isnot(None).label("has_embedding"),
        )
        .outerjoin(
            KbChunk,
            (KbChunk.source_type == "session") & (KbChunk.source_id == KbSession.id),
        )
        .order_by(KbSession.created_at.desc())
        .limit(min(limit, 100))
    )

    if project_key:
        q = q.where(KbSession.project_key == project_key)

    rows = await db.execute(q)
    entries: list[RecentEntry] = []
    for row in rows:
        preview = (row.normalized_log or "")[:200]
        if len(row.normalized_log or "") > 200:
            preview += "..."
        entries.append(
            RecentEntry(
                session_id=row.id,
                project_key=row.project_key,
                tool=row.tool,
                status=row.status,
                environment=row.environment,
                tags=row.tags or [],
                content_preview=preview,
                created_at=row.created_at,
                has_embedding=bool(row.has_embedding),
            )
        )

    return RecentResponse(entries=entries, total=len(entries))
