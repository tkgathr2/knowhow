from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import KbChunk

router = APIRouter(tags=["intelligence"])


class DecayRequest(BaseModel):
    days_threshold: int = 90
    decay_factor: float = 0.95
    min_confidence: float = 0.1
    dry_run: bool = False


class DecayResponse(BaseModel):
    affected_count: int
    dry_run: bool
    message: str


@router.post("/intelligence/decay", response_model=DecayResponse)
async def run_decay(
    req: DecayRequest, db: AsyncSession = Depends(get_db)
) -> DecayResponse:
    cutoff = datetime.now(UTC) - timedelta(days=req.days_threshold)

    count_q = select(func.count(KbChunk.id)).where(
        KbChunk.is_deprecated.is_(False),
        KbChunk.confidence_score > req.min_confidence,
        (KbChunk.last_recalled_at.is_(None)) | (KbChunk.last_recalled_at < cutoff),
        KbChunk.created_at < cutoff,
    )
    result = await db.execute(count_q)
    affected = result.scalar() or 0

    if not req.dry_run and affected > 0:
        await db.execute(
            update(KbChunk)
            .where(
                KbChunk.is_deprecated.is_(False),
                KbChunk.confidence_score > req.min_confidence,
                (KbChunk.last_recalled_at.is_(None)) | (KbChunk.last_recalled_at < cutoff),
                KbChunk.created_at < cutoff,
            )
            .values(
                confidence_score=func.greatest(
                    req.min_confidence,
                    KbChunk.confidence_score * req.decay_factor,
                )
            )
        )
        await db.commit()

    return DecayResponse(
        affected_count=affected,
        dry_run=req.dry_run,
        message=f"{affected}件のチャンクが{'対象' if req.dry_run else '減衰済み'}",
    )


class DuplicateCandidate(BaseModel):
    chunk_id_a: int
    chunk_id_b: int
    similarity: float
    project_key: str
    preview_a: str
    preview_b: str


class DuplicatesResponse(BaseModel):
    candidates: list[DuplicateCandidate]
    total: int


@router.get("/intelligence/duplicates", response_model=DuplicatesResponse)
async def find_duplicates(
    project_key: str | None = None,
    threshold: float = 0.95,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> DuplicatesResponse:
    safe_limit = min(limit, 100)
    params: dict = {"threshold": threshold, "lim": safe_limit}

    where_clause = ""
    if project_key:
        where_clause = "AND a.project_key = :pk AND b.project_key = :pk"
        params["pk"] = project_key

    q = text(f"""
        SELECT id_a, id_b, similarity, project_key, preview_a, preview_b
        FROM (
            SELECT a.id AS id_a, b.id AS id_b,
                   1 - (a.embedding <=> b.embedding) AS similarity,
                   a.project_key,
                   LEFT(a.content, 100) AS preview_a,
                   LEFT(b.content, 100) AS preview_b
            FROM kb_chunks a
            JOIN kb_chunks b ON a.id < b.id
                 AND a.project_key = b.project_key
            WHERE a.embedding IS NOT NULL
                  AND b.embedding IS NOT NULL
                  AND a.is_deprecated = false
                  AND b.is_deprecated = false
                  {where_clause}
        ) sub
        WHERE similarity >= :threshold
        ORDER BY similarity DESC
        LIMIT :lim
    """)

    try:
        rows = await db.execute(q, params)
        candidates = [
            DuplicateCandidate(
                chunk_id_a=row.id_a,
                chunk_id_b=row.id_b,
                similarity=float(row.similarity),
                project_key=row.project_key,
                preview_a=row.preview_a or "",
                preview_b=row.preview_b or "",
            )
            for row in rows
        ]
    except Exception:
        candidates = []
    return DuplicatesResponse(candidates=candidates, total=len(candidates))


class RecallStatEntry(BaseModel):
    project_key: str
    total_recalls: int
    avg_score: float | None
    avg_result_count: float | None
    zero_result_count: int


class RecallStatsResponse(BaseModel):
    stats: list[RecallStatEntry]
    total_recalls: int
    overall_avg_score: float | None


@router.get("/intelligence/recall-stats", response_model=RecallStatsResponse)
async def get_recall_stats(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
) -> RecallStatsResponse:
    cutoff = datetime.now(UTC) - timedelta(days=days)

    q = text("""
        SELECT project_key,
               COUNT(*) AS total_recalls,
               AVG(top_score) AS avg_score,
               AVG(result_count) AS avg_result_count,
               SUM(CASE WHEN result_count = 0 THEN 1 ELSE 0 END) AS zero_result_count
        FROM kb_recall_log
        WHERE created_at >= :cutoff
        GROUP BY project_key
        ORDER BY total_recalls DESC
    """)

    rows = await db.execute(q, {"cutoff": cutoff})
    stats = []
    total = 0
    score_sum = 0.0
    score_count = 0
    for row in rows:
        stats.append(
            RecallStatEntry(
                project_key=row.project_key,
                total_recalls=row.total_recalls,
                avg_score=float(row.avg_score) if row.avg_score else None,
                avg_result_count=float(row.avg_result_count) if row.avg_result_count else None,
                zero_result_count=row.zero_result_count,
            )
        )
        total += row.total_recalls
        if row.avg_score:
            score_sum += float(row.avg_score) * row.total_recalls
            score_count += row.total_recalls

    return RecallStatsResponse(
        stats=stats,
        total_recalls=total,
        overall_avg_score=score_sum / score_count if score_count > 0 else None,
    )


class MergeRequest(BaseModel):
    keep_chunk_id: int
    remove_chunk_id: int


class MergeResponse(BaseModel):
    kept_chunk_id: int
    removed_chunk_id: int
    message: str


@router.post("/intelligence/merge-duplicates", response_model=MergeResponse)
async def merge_duplicates(
    req: MergeRequest, db: AsyncSession = Depends(get_db)
) -> MergeResponse:
    from fastapi import HTTPException

    keep_row = await db.execute(select(KbChunk).where(KbChunk.id == req.keep_chunk_id))
    keep = keep_row.scalar_one_or_none()
    if not keep:
        raise HTTPException(status_code=404, detail="keep_chunk_id not found")

    remove_row = await db.execute(select(KbChunk).where(KbChunk.id == req.remove_chunk_id))
    remove = remove_row.scalar_one_or_none()
    if not remove:
        raise HTTPException(status_code=404, detail="remove_chunk_id not found")

    keep.helpful_count += remove.helpful_count
    keep.unhelpful_count += remove.unhelpful_count
    keep.recall_count += remove.recall_count
    keep.alpha += remove.alpha - 1.0
    keep.beta += remove.beta - 1.0
    keep.confidence_score = float(keep.alpha / (keep.alpha + keep.beta))

    merged_tags = list(set((keep.tags or []) + (remove.tags or [])))
    keep.tags = merged_tags

    remove.is_deprecated = True

    await db.commit()
    return MergeResponse(
        kept_chunk_id=keep.id,
        removed_chunk_id=remove.id,
        message="統合完了（重複チャンクを非推奨化）",
    )


class SummaryRequest(BaseModel):
    project_key: str | None = None
    top_k: int = 20


class SummaryResponse(BaseModel):
    summary: str
    chunk_count: int
    project_keys: list[str]


@router.post("/intelligence/summary", response_model=SummaryResponse)
async def generate_summary(
    req: SummaryRequest, db: AsyncSession = Depends(get_db)
) -> SummaryResponse:
    q = (
        select(KbChunk.project_key, KbChunk.content, KbChunk.tags, KbChunk.confidence_score)
        .where(KbChunk.is_deprecated.is_(False))
        .order_by(KbChunk.confidence_score.desc(), KbChunk.recall_count.desc())
        .limit(req.top_k)
    )
    if req.project_key:
        q = q.where(KbChunk.project_key == req.project_key)

    rows = await db.execute(q)
    chunks = list(rows)

    if not chunks:
        return SummaryResponse(summary="知見なし", chunk_count=0, project_keys=[])

    project_keys = list({row.project_key for row in chunks})

    lines = []
    for row in chunks:
        tags_str = ", ".join(row.tags or [])
        preview = row.content[:150].replace("\n", " ")
        lines.append(f"[{row.project_key}] ({tags_str}) {preview}")

    summary_input = "\n".join(lines)

    try:
        from openai import AsyncOpenAI

        from app.config import settings

        if settings.openai_api_key:
            client = AsyncOpenAI(api_key=settings.openai_api_key)
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "あなたは開発ナレッジの要約担当です。"
                            "以下の知見リストを日本語で簡潔に要約してください。"
                            "重要なパターン、よくある問題、ベストプラクティスを抽出してください。"
                        ),
                    },
                    {"role": "user", "content": summary_input},
                ],
                max_tokens=500,
            )
            summary = resp.choices[0].message.content or "要約生成失敗"
        else:
            summary = f"要約（AI未接続）:\n{summary_input[:500]}"
    except Exception:
        summary = f"要約（AI未接続）:\n{summary_input[:500]}"

    return SummaryResponse(
        summary=summary,
        chunk_count=len(chunks),
        project_keys=project_keys,
    )


class ChunkTopEntry(BaseModel):
    chunk_id: int
    project_key: str
    recall_count: int
    helpful_count: int
    confidence_score: float
    preview: str
    tags: list[str]


class TopChunksResponse(BaseModel):
    chunks: list[ChunkTopEntry]
    total: int


@router.get("/intelligence/top-chunks", response_model=TopChunksResponse)
async def get_top_chunks(
    sort_by: str = "recall_count",
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> TopChunksResponse:
    safe_limit = min(limit, 100)
    q = (
        select(
            KbChunk.id,
            KbChunk.project_key,
            KbChunk.recall_count,
            KbChunk.helpful_count,
            KbChunk.confidence_score,
            KbChunk.content,
            KbChunk.tags,
        )
        .where(KbChunk.is_deprecated.is_(False))
    )

    if sort_by == "helpful":
        q = q.order_by(KbChunk.helpful_count.desc())
    elif sort_by == "confidence":
        q = q.order_by(KbChunk.confidence_score.desc())
    else:
        q = q.order_by(KbChunk.recall_count.desc())

    q = q.limit(safe_limit)
    rows = await db.execute(q)
    chunks = [
        ChunkTopEntry(
            chunk_id=row.id,
            project_key=row.project_key,
            recall_count=row.recall_count,
            helpful_count=row.helpful_count,
            confidence_score=row.confidence_score,
            preview=row.content[:150],
            tags=row.tags or [],
        )
        for row in rows
    ]
    return TopChunksResponse(chunks=chunks, total=len(chunks))


# ── 不正UTF-8バイト行の検出・修復（22021対策・一回もの運用ツール）────────────
class RepairUtf8Request(BaseModel):
    dry_run: bool = True


class RepairUtf8Response(BaseModel):
    scanned: int
    broken_ids: list[int]
    repaired: int
    dry_run: bool
    note: str = ""


async def _id_is_broken(db: AsyncSession, chunk_id: int) -> bool:
    """文字関数を1行に当てて 22021 が出るか判定。失敗時はrollbackで掃除。"""
    from sqlalchemy.exc import DBAPIError

    try:
        await db.execute(
            text(
                "SELECT length(content) + coalesce(length(array_to_string(tags,'')),0) "
                "FROM kb_chunks WHERE id = :i"
            ),
            {"i": chunk_id},
        )
        return False
    except DBAPIError:
        await db.rollback()
        return True


async def _range_is_broken(db: AsyncSession, lo: int, hi: int) -> bool:
    from sqlalchemy.exc import DBAPIError

    try:
        await db.execute(
            text(
                "SELECT sum(length(content) + coalesce(length(array_to_string(tags,'')),0)) "
                "FROM kb_chunks WHERE id BETWEEN :lo AND :hi"
            ),
            {"lo": lo, "hi": hi},
        )
        return False
    except DBAPIError:
        await db.rollback()
        return True


def _repair_bytes(b: bytes) -> str:
    """必ず文字列化する修復。

    UTF-8 strict → （ほぼUTF-8なら）replaceで欠損文字だけ潰す →
    （大半が読めないバイト列なら）cp932 復元を試す → 最後は replace。
    truncated UTF-8（末尾欠け）を cp932 で誤復元して全文を壊さないための順序。
    """
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    replaced = b.decode("utf-8", errors="replace")
    n_bad = replaced.count("�")
    # 置換が2文字以下 or 全体の1割以下＝末尾欠け等の部分破損 → 欠損文字だけ置換
    if n_bad <= 2 or n_bad / max(1, len(replaced)) <= 0.1:
        return replaced
    try:
        return b.decode("cp932")  # 全体が別エンコーディング＝文字化け復元
    except UnicodeDecodeError:
        return replaced


@router.post("/intelligence/repair-utf8", response_model=RepairUtf8Response)
async def repair_utf8(
    req: RepairUtf8Request, db: AsyncSession = Depends(get_db)
) -> RepairUtf8Response:
    """kb_chunks 内の不正UTF-8バイト行（left/length等で22021を起こす行）を特定し修復する。

    検出は id 範囲の二分探索（壊れた範囲だけ深掘り）。修復は convert_to(…,'UTF8') の
    同一エンコーディング素通し（無変換）で生バイトを取り出し、cp932復元→replaceの順で再保存。
    """
    id_rows = await db.execute(text("SELECT min(id), max(id), count(*) FROM kb_chunks"))
    lo, hi, total = id_rows.one()
    if not total:
        return RepairUtf8Response(scanned=0, broken_ids=[], repaired=0, dry_run=req.dry_run)

    broken: list[int] = []

    async def _bisect(a: int, b: int) -> None:
        if a > b:
            return
        if not await _range_is_broken(db, a, b):
            return
        if a == b:
            broken.append(a)
            return
        mid = (a + b) // 2
        await _bisect(a, mid)
        await _bisect(mid + 1, b)

    await _bisect(int(lo), int(hi))

    repaired = 0
    if not req.dry_run:
        for cid in broken:
            row = (
                await db.execute(
                    text(
                        "SELECT convert_to(content,'UTF8') AS cb, "
                        "convert_to(coalesce(array_to_string(tags, E'\x1f'),''),'UTF8') AS tb "
                        "FROM kb_chunks WHERE id = :i"
                    ),
                    {"i": cid},
                )
            ).one_or_none()
            if row is None:
                continue
            content = _repair_bytes(bytes(row.cb or b""))
            tags_joined = _repair_bytes(bytes(row.tb or b""))
            tags = [t for t in tags_joined.split("\x1f") if t] if tags_joined else []
            await db.execute(
                update(KbChunk).where(KbChunk.id == cid).values(content=content, tags=tags)
            )
            repaired += 1
        await db.commit()

    return RepairUtf8Response(
        scanned=int(total),
        broken_ids=broken,
        repaired=repaired,
        dry_run=req.dry_run,
        note="修復後のembeddingは原文ほぼ同一のため再生成不要（置換文字のみ差分）",
    )
