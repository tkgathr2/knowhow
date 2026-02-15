from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import KbChunk, KbFeedback, KbIssue, KbProject, KbSession

router = APIRouter(tags=["feedback"])


class FeedbackRequest(BaseModel):
    project_key: str
    session_id: int
    query: str
    query_tags: list[str] = []
    returned_chunk_ids: list[int]
    selected_chunk_ids: list[int] = []
    resolved: bool
    was_helpful: str  # helpful | partial | unhelpful
    resolution_time_seconds: int | None = None
    notes: str | None = None


class FeedbackResponse(BaseModel):
    feedback_id: int
    message: str


def _now_utc() -> datetime:
    return datetime.now(UTC)


@router.post("/feedback", response_model=FeedbackResponse)
async def create_feedback(req: FeedbackRequest, db: AsyncSession = Depends(get_db)) -> FeedbackResponse:
    project_row = await db.execute(select(KbProject).where(KbProject.project_key == req.project_key))
    project = project_row.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{req.project_key}' not found")

    session_row = await db.execute(
        select(KbSession).where(KbSession.id == req.session_id, KbSession.project_key == req.project_key)
    )
    session = session_row.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{req.session_id}' not found")

    if req.was_helpful not in {"helpful", "partial", "unhelpful"}:
        raise HTTPException(status_code=400, detail="was_helpful must be one of: helpful, partial, unhelpful")

    feedback = KbFeedback(
        project_key=req.project_key,
        session_id=req.session_id,
        query=req.query,
        query_tags=req.query_tags,
        returned_chunk_ids=req.returned_chunk_ids,
        selected_chunk_ids=req.selected_chunk_ids,
        resolved=req.resolved,
        was_helpful=req.was_helpful,
        resolution_time_seconds=req.resolution_time_seconds,
        notes=req.notes,
    )
    db.add(feedback)

    now = _now_utc()

    selected_ids = set(req.selected_chunk_ids)
    returned_ids = set(req.returned_chunk_ids)
    unselected_returned_ids = returned_ids - selected_ids

    if req.was_helpful == "helpful":
        helpful_ids = selected_ids
        unhelpful_ids: set[int] = set()
        alpha_delta = 1.0
        beta_delta = 0.0
    elif req.was_helpful == "unhelpful":
        helpful_ids = set()
        unhelpful_ids = returned_ids
        alpha_delta = 0.0
        beta_delta = 1.0
    else:
        helpful_ids = selected_ids
        unhelpful_ids = unselected_returned_ids
        alpha_delta = 0.5
        beta_delta = 0.5

    if helpful_ids or unhelpful_ids:
        chunk_rows = await db.execute(select(KbChunk).where(KbChunk.id.in_(list(helpful_ids | unhelpful_ids))))
        chunks = list(chunk_rows.scalars())

        chunk_by_id = {c.id: c for c in chunks}

        for cid in helpful_ids:
            chunk = chunk_by_id.get(cid)
            if not chunk:
                continue
            chunk.helpful_count += 1
            chunk.alpha += alpha_delta
            chunk.last_helpful_at = now
            chunk.confidence_score = float(chunk.alpha / (chunk.alpha + chunk.beta))

        for cid in unhelpful_ids:
            chunk = chunk_by_id.get(cid)
            if not chunk:
                continue
            chunk.unhelpful_count += 1
            chunk.beta += beta_delta
            chunk.last_unhelpful_at = now
            chunk.confidence_score = float(chunk.alpha / (chunk.alpha + chunk.beta))

    await db.commit()
    await db.refresh(feedback)

    return FeedbackResponse(feedback_id=feedback.id, message="Feedback recorded")


class IssueCreateRequest(BaseModel):
    project_key: str
    chunk_id: int
    reason: str  # stale | wrong | env_mismatch | incomplete


class IssueCreateResponse(BaseModel):
    issue_id: int
    message: str


@router.post("/issues", response_model=IssueCreateResponse)
async def create_issue(req: IssueCreateRequest, db: AsyncSession = Depends(get_db)) -> IssueCreateResponse:
    project_row = await db.execute(select(KbProject).where(KbProject.project_key == req.project_key))
    project = project_row.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{req.project_key}' not found")

    chunk_row = await db.execute(
        select(KbChunk).where(KbChunk.id == req.chunk_id, KbChunk.project_key == req.project_key)
    )
    chunk = chunk_row.scalar_one_or_none()
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk '{req.chunk_id}' not found")

    if req.reason not in {"stale", "wrong", "env_mismatch", "incomplete"}:
        raise HTTPException(status_code=400, detail="reason must be one of: stale, wrong, env_mismatch, incomplete")

    issue = KbIssue(project_key=req.project_key, chunk_id=req.chunk_id, reason=req.reason, status="open")
    db.add(issue)
    await db.commit()
    await db.refresh(issue)

    return IssueCreateResponse(issue_id=issue.id, message="Issue created")


class ChunkDeprecateRequest(BaseModel):
    project_key: str
    chunk_id: int
    is_deprecated: bool = True


class ChunkDeprecateResponse(BaseModel):
    chunk_id: int
    is_deprecated: bool


@router.post("/chunks/deprecate", response_model=ChunkDeprecateResponse)
async def deprecate_chunk(req: ChunkDeprecateRequest, db: AsyncSession = Depends(get_db)) -> ChunkDeprecateResponse:
    chunk_row = await db.execute(
        select(KbChunk).where(KbChunk.id == req.chunk_id, KbChunk.project_key == req.project_key)
    )
    chunk = chunk_row.scalar_one_or_none()
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk '{req.chunk_id}' not found")

    chunk.is_deprecated = req.is_deprecated
    await db.commit()

    return ChunkDeprecateResponse(chunk_id=chunk.id, is_deprecated=chunk.is_deprecated)
