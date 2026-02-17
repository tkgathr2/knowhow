from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, text
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
    projects = {p.project_key: p for p in projects_q.scalars().all()}

    sess_agg = await db.execute(
        select(
            KbSession.project_key,
            func.count(KbSession.id).label("cnt"),
            func.max(KbSession.created_at).label("latest"),
        ).group_by(KbSession.project_key)
    )
    sess_map: dict[str, tuple[int, datetime | None]] = {}
    for row in sess_agg:
        sess_map[row.project_key] = (row.cnt, row.latest)

    chunk_agg = await db.execute(
        select(
            KbChunk.project_key,
            func.count(KbChunk.id).label("cnt"),
            func.count(
                case((KbChunk.embedding.isnot(None), KbChunk.id))
            ).label("embedded"),
        ).group_by(KbChunk.project_key)
    )
    chunk_map: dict[str, tuple[int, int]] = {}
    for row in chunk_agg:
        chunk_map[row.project_key] = (row.cnt, row.embedded)

    project_stats: list[ProjectStats] = []
    total_sessions = 0
    total_chunks = 0
    total_embedded = 0

    for pk, p in projects.items():
        sess_count, latest_at = sess_map.get(pk, (0, None))
        chunk_count, embedded_count = chunk_map.get(pk, (0, 0))
        project_stats.append(
            ProjectStats(
                project_key=pk,
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
    offset: int
    has_more: bool


@router.get("/recent", response_model=RecentResponse)
async def get_recent(
    limit: int = 20,
    offset: int = 0,
    project_key: str | None = None,
    tag: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RecentResponse:
    safe_limit = min(limit, 100)

    count_q = select(func.count(KbSession.id))
    if project_key:
        count_q = count_q.where(KbSession.project_key == project_key)
    if tag:
        count_q = count_q.where(KbSession.tags.any(tag))
    total_row = await db.execute(count_q)
    total_count = total_row.scalar() or 0

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
        .offset(offset)
        .limit(safe_limit)
    )

    if project_key:
        q = q.where(KbSession.project_key == project_key)
    if tag:
        q = q.where(KbSession.tags.any(tag))

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

    return RecentResponse(
        entries=entries,
        total=total_count,
        offset=offset,
        has_more=(offset + safe_limit) < total_count,
    )


class TagStat(BaseModel):
    tag: str
    count: int


class TagStatsResponse(BaseModel):
    tags: list[TagStat]
    total_tags: int


@router.get("/tags", response_model=TagStatsResponse)
async def get_tag_stats(
    project_key: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> TagStatsResponse:
    safe_limit = min(limit, 200)
    if project_key:
        rows = await db.execute(
            text(
                "SELECT t, COUNT(*) AS cnt "
                "FROM kb_chunks, unnest(tags) AS t "
                "WHERE project_key = :pk "
                "GROUP BY t ORDER BY cnt DESC LIMIT :lim"
            ),
            {"pk": project_key, "lim": safe_limit},
        )
    else:
        rows = await db.execute(
            text(
                "SELECT t, COUNT(*) AS cnt "
                "FROM kb_chunks, unnest(tags) AS t "
                "GROUP BY t ORDER BY cnt DESC LIMIT :lim"
            ),
            {"lim": safe_limit},
        )
    tags = [TagStat(tag=row[0], count=row[1]) for row in rows]
    return TagStatsResponse(tags=tags, total_tags=len(tags))


class ChunkDetail(BaseModel):
    chunk_id: int
    project_key: str
    content: str
    chunk_type: str
    tags: list[str]
    importance_score: int
    confidence_score: float
    helpful_count: int
    unhelpful_count: int
    is_deprecated: bool
    created_at: datetime


@router.get("/chunks/{chunk_id}", response_model=ChunkDetail)
async def get_chunk(
    chunk_id: int,
    db: AsyncSession = Depends(get_db),
) -> ChunkDetail:
    row = await db.execute(select(KbChunk).where(KbChunk.id == chunk_id))
    chunk = row.scalar_one_or_none()
    if not chunk:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Chunk not found")
    return ChunkDetail(
        chunk_id=chunk.id,
        project_key=chunk.project_key,
        content=chunk.content,
        chunk_type=chunk.chunk_type,
        tags=chunk.tags or [],
        importance_score=chunk.importance_score,
        confidence_score=chunk.confidence_score,
        helpful_count=chunk.helpful_count,
        unhelpful_count=chunk.unhelpful_count,
        is_deprecated=chunk.is_deprecated,
        created_at=chunk.created_at,
    )


class CrossProjectSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=10, ge=1, le=100)


class CrossProjectResult(BaseModel):
    chunk_id: int
    project_key: str
    content: str
    chunk_type: str
    score: float
    tags: list[str]


class CrossProjectSearchResponse(BaseModel):
    results: list[CrossProjectResult]
    query: str
    total: int


@router.post("/search/cross-project", response_model=CrossProjectSearchResponse)
async def cross_project_search(
    req: CrossProjectSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> CrossProjectSearchResponse:
    from app.embedding import clamp_top_k, create_embedding, escape_like

    top_k = clamp_top_k(req.top_k)
    results_by_id: dict[int, CrossProjectResult] = {}

    query_embedding = None
    try:
        query_embedding = await create_embedding(req.query)
    except Exception:
        pass

    base_where = [
        KbChunk.is_deprecated.is_(False),
        KbChunk.confidence_score >= 0.5,
    ]

    if query_embedding is not None:
        similarity = (
            1 - KbChunk.embedding.cosine_distance(query_embedding)
        ).label("similarity")
        vector_q = (
            select(
                KbChunk.id,
                KbChunk.project_key,
                KbChunk.content,
                KbChunk.chunk_type,
                KbChunk.tags,
                similarity,
            )
            .where(*base_where, KbChunk.embedding.isnot(None))
            .order_by(similarity.desc())
            .limit(top_k)
        )
        vector_rows = await db.execute(vector_q)
        for row in vector_rows:
            results_by_id[row.id] = CrossProjectResult(
                chunk_id=row.id,
                project_key=row.project_key,
                content=row.content,
                chunk_type=row.chunk_type,
                score=float(row.similarity),
                tags=row.tags or [],
            )

    if not results_by_id:
        escaped = escape_like(req.query)
        like_q = (
            select(
                KbChunk.id,
                KbChunk.project_key,
                KbChunk.content,
                KbChunk.chunk_type,
                KbChunk.tags,
                KbChunk.confidence_score,
            )
            .where(*base_where, KbChunk.content.ilike(f"%{escaped}%"))
            .order_by(KbChunk.confidence_score.desc())
            .limit(top_k)
        )
        like_rows = await db.execute(like_q)
        for row in like_rows:
            results_by_id[row.id] = CrossProjectResult(
                chunk_id=row.id,
                project_key=row.project_key,
                content=row.content,
                chunk_type=row.chunk_type,
                score=float(row.confidence_score) * 0.8,
                tags=row.tags or [],
            )

    results = sorted(
        results_by_id.values(), key=lambda r: r.score, reverse=True
    )[:top_k]
    return CrossProjectSearchResponse(
        results=results, query=req.query, total=len(results)
    )
