"""学びの新陳代謝（メタボライズ）API.

背景: 資産チャンクの大半が一度も想起されないまま滞留し、deprecated 化（代謝）が
ほぼゼロ。月次で Claude Code 側のスケジュールタスクが「候補取得 → LLMで蒸留
（要約・統合）→ 新カードを /api/devin/memorize で登録 → 旧チャンクを沈める」を
回すためのサーバ側 API を提供する。

エンドポイント（main.py で _protected ＝ KB_API_KEY 設定時 X-API-Key 必須）:
  GET  /api/admin/metabolize/candidates … 代謝候補の取得（読み取りのみ）
       - 一度も想起されていない資産チャンク（recall_count=0・非deprecated・
         作成から min_age_days 以上・source_type != 'webhook'）
       - 古い webhook 取込ログ（source_type='webhook'・非deprecated・同期間）
  POST /api/admin/metabolize/apply … 対象チャンクの一括 deprecated 化

安全設計:
  - 物理削除は一切しない。is_deprecated=True を立てるだけ（可逆）。
    巻き戻しは既存 POST /api/chunks/deprecate { is_deprecated: false } で1件ずつ可能。
  - meta に deprecated_reason / deprecated_at を非破壊で追記し、後から監査できる。
  - 1リクエストの対象は最大 MAX_APPLY_IDS 件。dry_run 対応。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import KbChunk

router = APIRouter(tags=["metabolize"])

# dashboard.py の _LOG_SOURCE と同じ区分（webhook=取込ログ / それ以外=正味の資産）
LOG_SOURCE = "webhook"

MAX_CANDIDATE_LIMIT = 200
MAX_APPLY_IDS = 500
DEFAULT_REASON = "metabolized"


# --- 純粋ロジック（DB非依存・単体テスト対象） --------------------------------

def meta_with_deprecation(meta: dict | None, reason: str, deprecated_at_iso: str) -> dict:
    """meta に代謝の監査情報を非破壊で追記した新しい dict を返す。

    元の meta は変更しない（JSONB はインプレース変更だと SQLAlchemy が
    変更検知しないため、必ず新 dict を代入する）。
    """
    base = dict(meta or {})
    base["deprecated_reason"] = reason
    base["deprecated_at"] = deprecated_at_iso
    return base


def normalize_reason(reason: str | None) -> str:
    """空・空白だけの reason は既定値 'metabolized' に寄せる。"""
    r = (reason or "").strip()
    return r if r else DEFAULT_REASON


def dedupe_ids(ids: list[int]) -> list[int]:
    """順序を保ったまま重複IDを除去する。"""
    seen: set[int] = set()
    out: list[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def partition_apply_results(
    requested_ids: list[int], found_by_id: dict[int, bool]
) -> tuple[list[int], list[int], list[int]]:
    """apply 対象を (deprecate対象, 既にdeprecated, 見つからない) の3つに分ける。

    found_by_id: {chunk_id: is_deprecated} ＝ DB で見つかったチャンクの現在状態。
    """
    to_deprecate: list[int] = []
    already: list[int] = []
    not_found: list[int] = []
    for cid in requested_ids:
        if cid not in found_by_id:
            not_found.append(cid)
        elif found_by_id[cid]:
            already.append(cid)
        else:
            to_deprecate.append(cid)
    return to_deprecate, already, not_found


# --- candidates -------------------------------------------------------------

class MetabolizeCandidate(BaseModel):
    id: int
    project_key: str
    source_type: str
    chunk_type: str
    content: str
    created_at: datetime
    recall_count: int
    confidence_score: float
    tags: list[str] = []


class CandidatesResponse(BaseModel):
    min_age_days: int
    limit: int
    asset_candidates: list[MetabolizeCandidate]   # 一度も想起されていない資産チャンク
    asset_total: int                              # limit 適用前の該当総数
    webhook_log_candidates: list[MetabolizeCandidate]  # 古い webhook 取込ログ
    webhook_log_total: int


def _to_candidate(c: KbChunk) -> MetabolizeCandidate:
    return MetabolizeCandidate(
        id=c.id,
        project_key=c.project_key,
        source_type=c.source_type,
        chunk_type=c.chunk_type,
        content=c.content,
        created_at=c.created_at,
        recall_count=c.recall_count,
        confidence_score=c.confidence_score,
        tags=c.tags or [],
    )


@router.get("/admin/metabolize/candidates", response_model=CandidatesResponse)
async def metabolize_candidates(
    min_age_days: int = Query(default=30, ge=0, le=3650),
    limit: int = Query(default=50, ge=1, le=MAX_CANDIDATE_LIMIT),
    project_key: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> CandidatesResponse:
    """代謝候補を返す（読み取りのみ・状態は変えない）。

    古い順（created_at 昇順）で返す＝最も長く滞留しているものから蒸留できる。
    """
    cutoff = datetime.now(UTC) - timedelta(days=min_age_days)

    base_where = [
        KbChunk.is_deprecated.is_(False),
        KbChunk.created_at < cutoff,
    ]
    if project_key:
        base_where.append(KbChunk.project_key == project_key)

    asset_where = [
        *base_where,
        KbChunk.source_type != LOG_SOURCE,
        KbChunk.recall_count == 0,
    ]
    log_where = [*base_where, KbChunk.source_type == LOG_SOURCE]

    asset_total = int(
        (await db.execute(select(func.count(KbChunk.id)).where(*asset_where))).scalar() or 0
    )
    log_total = int(
        (await db.execute(select(func.count(KbChunk.id)).where(*log_where))).scalar() or 0
    )

    asset_rows = await db.execute(
        select(KbChunk).where(*asset_where).order_by(KbChunk.created_at.asc()).limit(limit)
    )
    log_rows = await db.execute(
        select(KbChunk).where(*log_where).order_by(KbChunk.created_at.asc()).limit(limit)
    )

    return CandidatesResponse(
        min_age_days=min_age_days,
        limit=limit,
        asset_candidates=[_to_candidate(c) for c in asset_rows.scalars()],
        asset_total=asset_total,
        webhook_log_candidates=[_to_candidate(c) for c in log_rows.scalars()],
        webhook_log_total=log_total,
    )


# --- apply ------------------------------------------------------------------

class ApplyRequest(BaseModel):
    deprecate_chunk_ids: list[int] = Field(default_factory=list)
    reason: str = DEFAULT_REASON
    dry_run: bool = False


class ApplyResponse(BaseModel):
    requested: int
    deprecated: int
    already_deprecated: list[int]
    not_found: list[int]
    reason: str
    dry_run: bool
    message: str


@router.post("/admin/metabolize/apply", response_model=ApplyResponse)
async def metabolize_apply(
    req: ApplyRequest, db: AsyncSession = Depends(get_db)
) -> ApplyResponse:
    """対象チャンクを一括 deprecated 化する（物理削除はしない・可逆）。

    巻き戻し: 既存 POST /api/chunks/deprecate { is_deprecated: false }。
    """
    ids = dedupe_ids(req.deprecate_chunk_ids)
    if not ids:
        raise HTTPException(status_code=400, detail="deprecate_chunk_ids is empty")
    if len(ids) > MAX_APPLY_IDS:
        raise HTTPException(
            status_code=413,
            detail=f"Too many ids ({len(ids)} > {MAX_APPLY_IDS})",
        )

    reason = normalize_reason(req.reason)
    now_iso = datetime.now(UTC).isoformat()

    rows = await db.execute(select(KbChunk).where(KbChunk.id.in_(ids)))
    chunks = {c.id: c for c in rows.scalars()}
    found_by_id = {cid: c.is_deprecated for cid, c in chunks.items()}
    to_deprecate, already, not_found = partition_apply_results(ids, found_by_id)

    if not req.dry_run:
        for cid in to_deprecate:
            chunk = chunks[cid]
            chunk.is_deprecated = True
            # JSONB は新 dict を代入しないと変更検知されない（meta_with_deprecation は非破壊）
            chunk.meta = meta_with_deprecation(chunk.meta, reason, now_iso)
        await db.commit()

    return ApplyResponse(
        requested=len(ids),
        deprecated=len(to_deprecate),
        already_deprecated=already,
        not_found=not_found,
        reason=reason,
        dry_run=req.dry_run,
        message=(
            f"{len(to_deprecate)}件を非推奨化"
            + ("（dry-run・未反映）" if req.dry_run else "")
            + f" / 既に非推奨 {len(already)}件 / 不存在 {len(not_found)}件"
        ),
    )
