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


# ============================================================================
# Phase B: RRF (Reciprocal Rank Fusion) ハイブリッド検索（新規・追加のみ）
# 既存 /search・/devin/recall は無改変。検証後に各所を切替する想定（可逆）。
# 設計根拠: ベクトル(cosine)と全文(ts_rank_cd)を*両方*走らせ、ランクを 1/(k+rank) で
# 融合する。スコアのスケールが違うベクトルと全文を安全に統合でき、固有名詞/型番の
# 完全一致がベクトルの曖昧さに負ける典型失敗を防ぐ（RAGパネル提案・k=60が定番）。
# ============================================================================


def _vec_literal(emb: list[float]) -> str:
    """pgvector リテラル文字列 '[v1,v2,...]' に変換（CAST(:emb AS vector) 用）。"""
    return "[" + ",".join(repr(float(x)) for x in emb) + "]"


_RRF_SQL = text(
    """
    WITH vector_search AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:emb AS vector)) AS rank
        FROM kb_chunks
        WHERE project_key = :pk AND is_deprecated = false
          AND confidence_score >= :min_conf AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:emb AS vector)
        LIMIT :n_each
    ),
    fulltext_search AS (
        SELECT id, ROW_NUMBER() OVER (
            ORDER BY ts_rank_cd(search_vector, plainto_tsquery('simple', :q)) DESC
        ) AS rank
        FROM kb_chunks
        WHERE project_key = :pk AND is_deprecated = false AND confidence_score >= :min_conf
          AND search_vector @@ plainto_tsquery('simple', :q)
        ORDER BY ts_rank_cd(search_vector, plainto_tsquery('simple', :q)) DESC
        LIMIT :n_each
    )
    SELECT c.id, c.content, c.chunk_type, c.tags, c.source_type, c.source_id,
           c.importance_score, c.confidence_score,
           COALESCE(1.0/(:k + v.rank), 0.0) + COALESCE(1.0/(:k + f.rank), 0.0) AS rrf_score
    FROM kb_chunks c
    LEFT JOIN vector_search   v ON v.id = c.id
    LEFT JOIN fulltext_search f ON f.id = c.id
    WHERE v.id IS NOT NULL OR f.id IS NOT NULL
    ORDER BY rrf_score DESC
    LIMIT :n_final
    """
)

_FTS_ONLY_SQL = text(
    """
    SELECT c.id, c.content, c.chunk_type, c.tags, c.source_type, c.source_id,
           c.importance_score, c.confidence_score,
           ts_rank_cd(search_vector, plainto_tsquery('simple', :q)) AS rrf_score
    FROM kb_chunks c
    WHERE project_key = :pk AND is_deprecated = false AND confidence_score >= :min_conf
      AND search_vector @@ plainto_tsquery('simple', :q)
    ORDER BY rrf_score DESC
    LIMIT :n_final
    """
)


@router.post("/search/hybrid", response_model=SearchResponse)
async def search_hybrid(req: SearchRequest, db: AsyncSession = Depends(get_db)) -> SearchResponse:
    """RRF ハイブリッド検索（ベクトル＋全文をランク融合）。埋め込み不可時は全文のみ→ILIKE縮退。"""
    project_row = await db.execute(select(KbProject).where(KbProject.project_key == req.project_key))
    project = project_row.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{req.project_key}' not found")

    min_confidence = req.threshold or project.search_confidence_threshold
    top_k = clamp_top_k(req.top_k)
    n_each = min(max(top_k * 2, 20), 100)  # over-fetch（各リトリーバ20件以上）で融合を安定化
    k_rrf = 60  # RRF定数（定番）

    query_embedding = None
    try:
        query_embedding = await create_embedding(req.query)
    except Exception:
        query_embedding = None

    params = {
        "pk": req.project_key, "q": req.query, "min_conf": min_confidence,
        "n_each": n_each, "n_final": top_k, "k": k_rrf,
    }
    results: list[ChunkResult] = []
    try:
        if query_embedding is not None:
            params["emb"] = _vec_literal(query_embedding)
            rows = await db.execute(_RRF_SQL, params)
        else:
            rows = await db.execute(_FTS_ONLY_SQL, params)
        results = [
            ChunkResult(
                chunk_id=r.id, content=r.content, chunk_type=r.chunk_type,
                score=float(r.rrf_score), tags=r.tags or [], source_type=r.source_type,
                source_id=r.source_id, importance_score=r.importance_score,
                confidence_score=r.confidence_score,
            )
            for r in rows
        ]
    except Exception:
        results = []

    # 両方ゼロ件 → ILIKE フォールバック（既存と同じ縮退でゼロ件事故を防ぐ）
    if not results:
        escaped = escape_like(req.query)
        like_q = (
            _base_chunk_query(req.project_key, min_confidence)
            .where(KbChunk.content.ilike(f"%{escaped}%"))
            .order_by(KbChunk.confidence_score.desc(), KbChunk.importance_score.desc())
            .limit(top_k)
        )
        like_rows = await db.execute(like_q)
        results = [
            ChunkResult(
                chunk_id=row.id, content=row.content, chunk_type=row.chunk_type,
                score=float(row.confidence_score) * 0.8, tags=row.tags or [],
                source_type=row.source_type, source_id=row.source_id,
                importance_score=row.importance_score, confidence_score=row.confidence_score,
            )
            for row in like_rows
        ]

    return SearchResponse(results=results, query=req.query, total=len(results))
