import hashlib

from fastapi import APIRouter, Depends
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import KbChunk, KbProject, KbSession

router = APIRouter(tags=["bulk"])

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


class BulkEntry(BaseModel):
    project_key: str
    raw_log: str
    tool: str = "devin"
    status: str = "success"
    environment: str = "local"
    tags: list[str] = []


class BulkMemorizeRequest(BaseModel):
    entries: list[BulkEntry]


class BulkResultItem(BaseModel):
    index: int
    session_id: int | None
    chunk_id: int | None
    status: str


class BulkMemorizeResponse(BaseModel):
    total_submitted: int
    total_imported: int
    total_skipped: int
    results: list[BulkResultItem]


@router.post("/devin/bulk-memorize", response_model=BulkMemorizeResponse)
async def bulk_memorize(
    req: BulkMemorizeRequest, db: AsyncSession = Depends(get_db)
) -> BulkMemorizeResponse:
    results: list[BulkResultItem] = []
    imported = 0
    skipped = 0

    for idx, entry in enumerate(req.entries):
        project_row = await db.execute(
            select(KbProject).where(KbProject.project_key == entry.project_key)
        )
        project = project_row.scalar_one_or_none()
        if not project:
            project = KbProject(
                project_key=entry.project_key,
                display_name=entry.project_key,
            )
            db.add(project)
            await db.flush()

        log_hash = hashlib.sha256(entry.raw_log.encode("utf-8")).hexdigest()

        existing = await db.execute(
            select(KbSession).where(
                KbSession.project_key == entry.project_key,
                KbSession.hash == log_hash,
            )
        )
        if existing.scalar_one_or_none():
            skipped += 1
            results.append(
                BulkResultItem(
                    index=idx, session_id=None, chunk_id=None, status="skipped"
                )
            )
            continue

        session = KbSession(
            project_key=entry.project_key,
            tool=entry.tool,
            status=entry.status,
            environment=entry.environment,
            raw_log=entry.raw_log,
            normalized_log=entry.raw_log.strip(),
            tags=entry.tags,
            hash=log_hash,
            ingest_state="summarized",
        )
        db.add(session)
        await db.flush()

        chunk = KbChunk(
            project_key=entry.project_key,
            source_type="session",
            source_id=session.id,
            chunk_type="session_log",
            content=session.normalized_log,
            importance_score=5,
            confidence_score=0.9,
            alpha=9.0,
            beta=1.0,
            tags=entry.tags,
            meta={
                "tool": entry.tool,
                "status": entry.status,
                "environment": entry.environment,
                "bulk_import": True,
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

        await db.flush()
        imported += 1
        results.append(
            BulkResultItem(
                index=idx,
                session_id=session.id,
                chunk_id=chunk.id,
                status="imported",
            )
        )

    await db.commit()

    return BulkMemorizeResponse(
        total_submitted=len(req.entries),
        total_imported=imported,
        total_skipped=skipped,
        results=results,
    )
