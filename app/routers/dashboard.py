from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import case, distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import bucho as bucho_calc
from app import growth as growth_calc
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


# --- 成長ダッシュボード（HO: knowhow growth）---------------------------------
# source_type='webhook'（GitHub の push/pr 自動取込）を「取込ログ」、それ以外
# （session / learning / external / stackoverflow）を「正味のナレッジ資産」とみなす。
# 総量で成長を過大評価しないよう、資産とログを分けて集計する。
_LOG_SOURCE = "webhook"


class GrowthPoint(BaseModel):
    period: str
    added: int
    cumulative: int
    growth_pct: float | None
    deprecated_added: int


class GrowthCurrent(BaseModel):
    period: str
    added_so_far: int
    days_elapsed: int
    days_in_period: int
    projected_added: int
    projected_growth_pct: float | None


class GrowthTotals(BaseModel):
    chunks: int
    asset: int
    log: int
    deprecated: int
    embedded: int
    vectorized_pct: float
    avg_confidence: float | None
    helpful_rate: float | None
    recall_total: int
    recalled_chunks: int
    projects: int


class SourceTypeStat(BaseModel):
    source_type: str
    count: int


class GrowthResponse(BaseModel):
    bucket: str
    series: str
    baseline_period: str | None
    narrative: str
    points: list[GrowthPoint]
    current_period: GrowthCurrent | None
    totals: GrowthTotals
    by_source_type: list[SourceTypeStat]


@router.get("/stats/growth", response_model=GrowthResponse)
async def get_growth(
    bucket: str = "month",
    series: str = "all",
    project_key: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> GrowthResponse:
    if bucket not in ("month", "week"):
        bucket = "month"
    if series not in ("asset", "log", "all"):
        series = "all"

    trunc_unit = "week" if bucket == "week" else "month"
    label_fmt = 'IYYY-"W"IW' if bucket == "week" else "YYYY-MM"
    period_expr = func.to_char(
        func.date_trunc(trunc_unit, KbChunk.created_at), label_fmt
    )

    base_where = []
    if project_key:
        base_where.append(KbChunk.project_key == project_key)

    series_where = list(base_where)
    if series == "asset":
        series_where.append(KbChunk.source_type != _LOG_SOURCE)
    elif series == "log":
        series_where.append(KbChunk.source_type == _LOG_SOURCE)

    # 期間別の追加件数
    added_rows = await db.execute(
        select(period_expr.label("period"), func.count(KbChunk.id).label("cnt"))
        .where(*series_where)
        .group_by(period_expr)
    )
    added_by_period = {row.period: row.cnt for row in added_rows if row.period}

    # 期間別の非推奨化（新陳代謝）件数：作成期間ごとに、現在 is_deprecated の数
    dep_rows = await db.execute(
        select(period_expr.label("period"), func.count(KbChunk.id).label("cnt"))
        .where(*series_where, KbChunk.is_deprecated.is_(True))
        .group_by(period_expr)
    )
    deprecated_by_period = {row.period: row.cnt for row in dep_rows if row.period}

    points = growth_calc.build_points(added_by_period, deprecated_by_period)
    now = datetime.now(timezone.utc)
    current = growth_calc.project_current(points, now, bucket)
    narrative = growth_calc.make_narrative(points, current, series)
    baseline_period = points[0]["period"] if points else None

    # 総量（series ではなく project_key だけで絞る＝資産/ログの全体像を見せる）
    totals_row = (
        await db.execute(
            select(
                func.count(KbChunk.id),
                func.count(case((KbChunk.source_type != _LOG_SOURCE, KbChunk.id))),
                func.count(case((KbChunk.source_type == _LOG_SOURCE, KbChunk.id))),
                func.count(case((KbChunk.is_deprecated.is_(True), KbChunk.id))),
                func.count(case((KbChunk.embedding.isnot(None), KbChunk.id))),
                func.avg(
                    case((KbChunk.source_type != _LOG_SOURCE, KbChunk.confidence_score))
                ),
                func.coalesce(func.sum(KbChunk.helpful_count), 0),
                func.coalesce(func.sum(KbChunk.unhelpful_count), 0),
                func.coalesce(func.sum(KbChunk.recall_count), 0),
                func.count(case((KbChunk.recall_count > 0, KbChunk.id))),
                func.count(distinct(KbChunk.project_key)),
            ).where(*base_where)
        )
    ).one()
    (
        t_chunks,
        t_asset,
        t_log,
        t_deprecated,
        t_embedded,
        t_avg_conf,
        t_helpful,
        t_unhelpful,
        t_recall,
        t_recalled_chunks,
        t_projects,
    ) = totals_row

    totals = GrowthTotals(
        chunks=t_chunks,
        asset=t_asset,
        log=t_log,
        deprecated=t_deprecated,
        embedded=t_embedded,
        vectorized_pct=growth_calc.vectorized_pct(t_embedded, t_chunks),
        avg_confidence=(
            round(float(t_avg_conf) * 100, 1) if t_avg_conf is not None else None
        ),
        helpful_rate=growth_calc.helpful_rate(t_helpful, t_unhelpful),
        recall_total=t_recall,
        recalled_chunks=t_recalled_chunks,
        projects=t_projects,
    )

    st_rows = await db.execute(
        select(KbChunk.source_type, func.count(KbChunk.id).label("cnt"))
        .where(*base_where)
        .group_by(KbChunk.source_type)
        .order_by(func.count(KbChunk.id).desc())
    )
    by_source_type = [
        SourceTypeStat(source_type=row.source_type, count=row.cnt) for row in st_rows
    ]

    return GrowthResponse(
        bucket=bucket,
        series=series,
        baseline_period=baseline_period,
        narrative=narrative,
        points=[GrowthPoint(**p) for p in points],
        current_period=GrowthCurrent(**current) if current else None,
        totals=totals,
        by_source_type=by_source_type,
    )


# --- 日次の成長ログ（毎日「何が」増えた/使われた/沈んだか）-------------------
class DailyItem(BaseModel):
    chunk_id: int
    project_key: str
    chunk_type: str
    source_type: str
    preview: str
    tags: list[str]
    confidence: float
    recall_count: int
    is_deprecated: bool
    created_at: datetime


class DailyEntry(BaseModel):
    date: str
    asset_added: int
    log_added: int
    deprecated: int
    recalled: int
    asset_cumulative: int = 0
    growth_pct: float | None = None
    items: list[DailyItem]
    items_truncated: int


class DailyLatest(BaseModel):
    date: str | None
    asset_added: int
    asset_cumulative: int
    growth_pct: float | None


class DailyResponse(BaseModel):
    days: int
    since: str
    latest: DailyLatest | None = None
    entries: list[DailyEntry]


def _day_expr(col):
    return func.to_char(func.date_trunc("day", col), "YYYY-MM-DD")


@router.get("/growth/daily", response_model=DailyResponse)
async def get_growth_daily(
    days: int = 14,
    project_key: str | None = None,
    light: bool = False,
    db: AsyncSession = Depends(get_db),
) -> DailyResponse:
    # light=True: 重い「中身（最大600件・本文取得）」を省き、数字とチップだけ即返す。
    # 画面の初回描画を待たせない用途（中身は後追いで full 呼び出しが埋める）。
    days = max(1, min(days, 60))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    where = [KbChunk.created_at >= since]
    if project_key:
        where.append(KbChunk.project_key == project_key)

    created_day = _day_expr(KbChunk.created_at)

    # 数字パート（日次カウント・前日累計・想起）は1往復にまとめる。
    # 実測: app↔DB は1クエリ往復あたり数百ms（クエリの軽重でなく往復回数が効く）。
    # 以前は別々の3クエリ＝3往復だった。CTEで1文にして1往復で全部取る。
    # content には触れないので 22021（不正UTF-8）の心配は無い。
    pk_clause = "AND project_key = :pk" if project_key else ""
    bundle_sql = text(
        f"""
        WITH win AS (
            SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS d,
                   count(*) FILTER (WHERE source_type <> :log_src) AS asset,
                   count(*) FILTER (WHERE source_type =  :log_src) AS log,
                   count(*) FILTER (WHERE is_deprecated)           AS dep
            FROM kb_chunks
            WHERE created_at >= :since {pk_clause}
            GROUP BY 1
        ),
        rec AS (
            SELECT to_char(date_trunc('day', last_recalled_at), 'YYYY-MM-DD') AS d,
                   count(*) AS c
            FROM kb_chunks
            WHERE last_recalled_at IS NOT NULL AND last_recalled_at >= :since {pk_clause}
            GROUP BY 1
        ),
        base AS (
            SELECT count(*) AS c
            FROM kb_chunks
            WHERE created_at < :since AND source_type <> :log_src {pk_clause}
        )
        SELECT 'win'::text AS kind, d, asset, log, dep, NULL::bigint AS rc FROM win
        UNION ALL
        SELECT 'rec'::text, d, NULL, NULL, NULL, c FROM rec
        UNION ALL
        SELECT 'base'::text, NULL, NULL, NULL, NULL, c FROM base
        """
    )
    params: dict = {"since": since, "log_src": _LOG_SOURCE}
    if project_key:
        params["pk"] = project_key
    bundle = await db.execute(bundle_sql, params)

    asset_by_day: dict[str, int] = {}
    log_by_day: dict[str, int] = {}
    dep_by_day: dict[str, int] = {}
    recalled_by_day: dict[str, int] = {}
    base_before = 0
    for row in bundle:
        if row.kind == "win":
            if not row.d:
                continue
            if row.asset:
                asset_by_day[row.d] = int(row.asset)
            if row.log:
                log_by_day[row.d] = int(row.log)
            if row.dep:
                dep_by_day[row.d] = int(row.dep)
        elif row.kind == "rec":
            if row.d:
                recalled_by_day[row.d] = int(row.rc)
        else:  # base
            base_before = int(row.rc or 0)

    # その日に増えた「正味のナレッジ資産」の中身（新しい順）。
    # ここが最重）— 最大600行の本文(content)取得で、並行トラフィック時に
    # キャッシュが落ちると数百ms〜1秒超かかる。light では丸ごと省いて即返す。
    items_by_day: dict[str, list[DailyItem]] = {}
    if not light:
        item_rows = await db.execute(
            select(
                KbChunk.id,
                KbChunk.project_key,
                KbChunk.chunk_type,
                KbChunk.source_type,
                KbChunk.content,
                KbChunk.tags,
                KbChunk.confidence_score,
                KbChunk.recall_count,
                KbChunk.is_deprecated,
                KbChunk.created_at,
                created_day.label("d"),
            )
            .where(*where, KbChunk.source_type != _LOG_SOURCE)
            .order_by(KbChunk.created_at.desc())
            .limit(600)
        )
        for row in item_rows:
            preview = (row.content or "")[:160]
            if len(row.content or "") > 160:
                preview += "…"
            items_by_day.setdefault(row.d, []).append(
                DailyItem(
                    chunk_id=row.id,
                    project_key=row.project_key,
                    chunk_type=row.chunk_type,
                    source_type=row.source_type,
                    preview=preview,
                    tags=row.tags or [],
                    confidence=round(float(row.confidence_score) * 100, 1),
                    recall_count=row.recall_count,
                    is_deprecated=row.is_deprecated,
                    created_at=row.created_at,
                )
            )

    days_desc = growth_calc.daily_keys(
        asset_by_day, log_by_day, dep_by_day, recalled_by_day, items_by_day
    )
    entries_raw = growth_calc.assemble_daily(
        days_desc, asset_by_day, log_by_day, dep_by_day, recalled_by_day, items_by_day
    )
    entries_raw = growth_calc.attach_daily_growth(entries_raw, base_before)
    latest_raw = growth_calc.latest_daily_growth(entries_raw)
    entries = [
        DailyEntry(
            date=e["date"],
            asset_added=e["asset_added"],
            log_added=e["log_added"],
            deprecated=e["deprecated"],
            recalled=e["recalled"],
            asset_cumulative=e["asset_cumulative"],
            growth_pct=e["growth_pct"],
            items=e["items"],
            items_truncated=e["items_truncated"],
        )
        for e in entries_raw
    ]
    return DailyResponse(
        days=days,
        since=since.strftime("%Y-%m-%d"),
        latest=DailyLatest(**latest_raw) if latest_raw else None,
        entries=entries,
    )


# --- ②使う：想起(recall)活用パネル -----------------------------------------
class TopRecalled(BaseModel):
    chunk_id: int
    project_key: str
    chunk_type: str
    preview: str
    recall_count: int
    last_recalled_at: datetime | None


class ProjectRecall(BaseModel):
    project_key: str
    recalls: int


class UsageStats(BaseModel):
    total_recalls: int
    recalled_chunks: int
    asset_chunks: int
    never_recalled: int
    utilization_pct: float
    recent_active: int
    by_project: list[ProjectRecall]
    top_recalled: list[TopRecalled]


@router.get("/stats/usage", response_model=UsageStats)
async def get_usage(
    days: int = 30, db: AsyncSession = Depends(get_db)
) -> UsageStats:
    days = max(1, min(days, 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    asset = KbChunk.source_type != _LOG_SOURCE

    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(KbChunk.recall_count), 0),
                func.count(case((KbChunk.recall_count > 0, KbChunk.id))),
                func.count(case((asset, KbChunk.id))),
                func.count(case((asset & (KbChunk.recall_count == 0), KbChunk.id))),
                func.count(case((KbChunk.last_recalled_at >= since, KbChunk.id))),
            )
        )
    ).one()
    total_recalls, recalled_chunks, asset_chunks, never_recalled, recent_active = row

    proj_rows = (
        await db.execute(
            select(KbChunk.project_key, func.coalesce(func.sum(KbChunk.recall_count), 0))
            .where(KbChunk.recall_count > 0)
            .group_by(KbChunk.project_key)
            .order_by(func.coalesce(func.sum(KbChunk.recall_count), 0).desc())
            .limit(10)
        )
    ).all()
    by_project = [ProjectRecall(project_key=pk, recalls=int(c)) for pk, c in proj_rows]

    top_rows = (
        await db.execute(
            select(
                KbChunk.id, KbChunk.project_key, KbChunk.chunk_type,
                KbChunk.content, KbChunk.recall_count, KbChunk.last_recalled_at,
            )
            .where(asset, KbChunk.recall_count > 0)
            .order_by(KbChunk.recall_count.desc(), KbChunk.last_recalled_at.desc())
            .limit(10)
        )
    ).all()
    top_recalled = [
        TopRecalled(
            chunk_id=cid, project_key=pk, chunk_type=ct,
            preview=((content or "")[:140] + ("…" if len(content or "") > 140 else "")),
            recall_count=rc, last_recalled_at=lra,
        )
        for cid, pk, ct, content, rc, lra in top_rows
    ]

    return UsageStats(
        total_recalls=int(total_recalls),
        recalled_chunks=recalled_chunks,
        asset_chunks=asset_chunks,
        never_recalled=never_recalled,
        utilization_pct=growth_calc.vectorized_pct(recalled_chunks, asset_chunks),
        recent_active=recent_active,
        by_project=by_project,
        top_recalled=top_recalled,
    )


# --- ③賢く×週次：成長サマリ（cronが叩いて要約を返す）-----------------------
class WeeklySummary(BaseModel):
    week_start: str
    week_end: str
    asset_now: int
    asset_prev: int
    recalls_now: int
    helpful_rate: float | None
    deprecated_now: int
    utilization_pct: float
    never_recalled: int
    narrative: str


@router.get("/stats/weekly-summary", response_model=WeeklySummary)
async def get_weekly_summary(db: AsyncSession = Depends(get_db)) -> WeeklySummary:
    now = datetime.now(timezone.utc)
    w1 = now - timedelta(days=7)
    w2 = now - timedelta(days=14)
    asset = KbChunk.source_type != _LOG_SOURCE

    row = (
        await db.execute(
            select(
                func.count(case((asset & (KbChunk.created_at >= w1), KbChunk.id))),
                func.count(
                    case((asset & (KbChunk.created_at >= w2) & (KbChunk.created_at < w1), KbChunk.id))
                ),
                func.count(case((KbChunk.last_recalled_at >= w1, KbChunk.id))),
                func.count(case((asset & (KbChunk.created_at >= w1) & KbChunk.is_deprecated.is_(True), KbChunk.id))),
                func.coalesce(func.sum(KbChunk.helpful_count), 0),
                func.coalesce(func.sum(KbChunk.unhelpful_count), 0),
                func.count(case((asset, KbChunk.id))),
                func.count(case((asset & (KbChunk.recall_count > 0), KbChunk.id))),
            )
        )
    ).one()
    asset_now, asset_prev, recalls_now, deprecated_now, helpful, unhelpful, asset_total, recalled = row

    helpful_rate = growth_calc.helpful_rate(helpful, unhelpful)
    util = growth_calc.vectorized_pct(recalled, asset_total)
    never = asset_total - recalled
    narrative = growth_calc.make_weekly_narrative(
        {
            "asset_now": asset_now, "asset_prev": asset_prev, "recalls_now": recalls_now,
            "helpful_rate": helpful_rate, "deprecated_now": deprecated_now,
            "util_pct": util, "never_recalled": never,
        }
    )
    return WeeklySummary(
        week_start=w1.strftime("%Y-%m-%d"),
        week_end=now.strftime("%Y-%m-%d"),
        asset_now=asset_now,
        asset_prev=asset_prev,
        recalls_now=recalls_now,
        helpful_rate=helpful_rate,
        deprecated_now=deprecated_now,
        utilization_pct=util,
        never_recalled=never,
        narrative=narrative,
    )


# --- 部長別の成長（5部長制・社長指示 2026/06/13） -----------------------------
class BuchoCard(BaseModel):
    key: str
    name: str
    title: str
    emoji: str
    domain: str
    color: str
    total: int
    added: int
    added_prev: int
    growth_pct: float | None
    recalls: int
    d1: int = 0
    d7: int = 0
    d30: int = 0
    d1_pct: float | None = None
    d7_pct: float | None = None
    d30_pct: float | None = None


class BuchoResponse(BaseModel):
    days: int
    since: str
    buchos: list[BuchoCard]


@router.get("/stats/bucho", response_model=BuchoResponse)
async def get_bucho_stats(days: int = 30, db: AsyncSession = Depends(get_db)) -> BuchoResponse:
    """ナレッジ資産を5部長＋全社共通に分類して、部長ごとの伸びを返す。

    分類は project_key の明示マップ＋タグ/本文キーワード（app/bucho.py）。
    取込ログ(webhook)と非推奨は数えない＝「正味の知恵」だけを部長の成長とみなす。
    """
    days = max(1, min(days, 365))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    prev_since = now - timedelta(days=days * 2)
    # 社長の比較ビュー：昨日(1日)・1週間・1か月の増えた数と前同期間比%（期間ボタンとは独立）
    compare_windows = [
        {"key": "d1",
         "since": (now - timedelta(days=1)).isoformat(),
         "prev_since": (now - timedelta(days=2)).isoformat()},
        {"key": "d7",
         "since": (now - timedelta(days=7)).isoformat(),
         "prev_since": (now - timedelta(days=14)).isoformat()},
        {"key": "d30",
         "since": (now - timedelta(days=30)).isoformat(),
         "prev_since": (now - timedelta(days=60)).isoformat()},
    ]

    # NOTE: SQL側の left()/substr() は使わない。本DBでは文字関数がバイト単位で
    # 切れて不正UTF-8を生成し22021になる（2026-06-13 本番事故で実証）。
    # 全文を取得して Python 側で切る（/growth/daily と同じ安全パターン）。
    rows = await db.execute(
        select(
            KbChunk.project_key,
            KbChunk.tags,
            KbChunk.content,
            KbChunk.created_at,
            KbChunk.recall_count,
        ).where(KbChunk.source_type != _LOG_SOURCE, KbChunk.is_deprecated.is_(False))
    )
    data = [
        {
            "project_key": r.project_key,
            "tags": r.tags or [],
            "content_head": (r.content or "")[:200],
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "recall_count": r.recall_count or 0,
        }
        for r in rows
    ]
    buchos = bucho_calc.aggregate(data, since.isoformat(), prev_since.isoformat())
    compare = bucho_calc.aggregate_compare(data, compare_windows)
    for b in buchos:
        b.update(compare.get(b["key"], {}))
    return BuchoResponse(
        days=days,
        since=since.strftime("%Y-%m-%d"),
        buchos=[BuchoCard(**b) for b in buchos],
    )


class BuchoDetailResponse(BaseModel):
    key: str
    name: str
    title: str
    emoji: str
    domain: str
    color: str
    days: int
    since: str
    total: int
    added: int
    added_prev: int
    growth_pct: float | None
    recalls: int
    monthly: list[dict]
    recent_items: list[dict]
    top_recalled: list[dict]
    top_projects: list[dict]


@router.get("/bucho/{key}", response_model=BuchoDetailResponse)
async def get_bucho_detail(
    key: str, days: int = 30, db: AsyncSession = Depends(get_db)
) -> BuchoDetailResponse:
    """1部長分の成長詳細（個別ページ用）。分類ロジックは /stats/bucho と同一。"""
    if bucho_calc.bucho_def(key) is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="unknown bucho key")

    days = max(1, min(days, 365))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    prev_since = now - timedelta(days=days * 2)

    # SQL側で文字列を切らない（left()はバイト切りで22021・本ファイル stats/bucho 参照）
    rows = await db.execute(
        select(
            KbChunk.id,
            KbChunk.project_key,
            KbChunk.tags,
            KbChunk.content,
            KbChunk.created_at,
            KbChunk.recall_count,
        ).where(KbChunk.source_type != _LOG_SOURCE, KbChunk.is_deprecated.is_(False))
    )
    data = [
        {
            "chunk_id": r.id,
            "project_key": r.project_key,
            "tags": r.tags or [],
            "content_head": (r.content or "")[:200],
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "recall_count": r.recall_count or 0,
        }
        for r in rows
    ]
    det = bucho_calc.detail(
        data, key, since.isoformat(), prev_since.isoformat(), now.strftime("%Y-%m")
    )
    return BuchoDetailResponse(days=days, since=since.strftime("%Y-%m-%d"), **det)
