import hashlib
import re
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.embedding import create_embedding
from app.models import KbChunk, KbExternalSource, KbProject

router = APIRouter(tags=["external"])

_HTTPX_TIMEOUT = 30.0


class SourceCreate(BaseModel):
    source_type: str
    source_url: str
    project_key: str | None = None
    config: dict = {}


class SourceResponse(BaseModel):
    id: int
    source_type: str
    source_url: str
    project_key: str | None
    is_active: bool
    sync_count: int
    last_synced_at: datetime | None


class SourceListResponse(BaseModel):
    sources: list[SourceResponse]
    total: int


@router.get("/external/sources", response_model=SourceListResponse)
async def list_sources(
    db: AsyncSession = Depends(get_db),
) -> SourceListResponse:
    rows = await db.execute(
        select(KbExternalSource).order_by(KbExternalSource.created_at.desc())
    )
    sources = [
        SourceResponse(
            id=s.id,
            source_type=s.source_type,
            source_url=s.source_url,
            project_key=s.project_key,
            is_active=s.is_active,
            sync_count=s.sync_count,
            last_synced_at=s.last_synced_at,
        )
        for s in rows.scalars()
    ]
    return SourceListResponse(sources=sources, total=len(sources))


@router.post("/external/sources", response_model=SourceResponse)
async def add_source(
    req: SourceCreate, db: AsyncSession = Depends(get_db)
) -> SourceResponse:
    existing = await db.execute(
        select(KbExternalSource).where(
            KbExternalSource.source_type == req.source_type,
            KbExternalSource.source_url == req.source_url,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Source already exists")

    src = KbExternalSource(
        source_type=req.source_type,
        source_url=req.source_url,
        project_key=req.project_key,
        config=req.config,
    )
    db.add(src)
    await db.commit()
    await db.refresh(src)
    return SourceResponse(
        id=src.id,
        source_type=src.source_type,
        source_url=src.source_url,
        project_key=src.project_key,
        is_active=src.is_active,
        sync_count=src.sync_count,
        last_synced_at=src.last_synced_at,
    )


async def _ensure_project(db: AsyncSession, project_key: str) -> None:
    row = await db.execute(
        select(KbProject).where(KbProject.project_key == project_key)
    )
    if not row.scalar_one_or_none():
        db.add(KbProject(project_key=project_key, display_name=project_key))
        await db.flush()


async def _store_external_chunk(
    db: AsyncSession,
    project_key: str,
    content: str,
    chunk_type: str,
    tags: list[str],
    meta: dict,
    source_id: int,
) -> KbChunk | None:
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    existing = await db.execute(
        select(KbChunk).where(
            KbChunk.project_key == project_key,
            KbChunk.meta["content_hash"].astext == content_hash,
        )
    )
    if existing.scalar_one_or_none():
        return None

    chunk = KbChunk(
        project_key=project_key,
        source_type="external",
        source_id=source_id,
        chunk_type=chunk_type,
        content=content,
        importance_score=4,
        confidence_score=0.7,
        alpha=7.0,
        beta=3.0,
        tags=tags,
        meta={**meta, "content_hash": content_hash},
    )
    db.add(chunk)

    try:
        embedding = await create_embedding(content)
        if embedding is not None:
            chunk.embedding = embedding
            chunk.embedding_model = settings.embedding_model
            chunk.embedding_dimensions = settings.embedding_dim
    except Exception:
        pass

    return chunk


class GithubIssuesRequest(BaseModel):
    repo: str
    project_key: str
    labels: str = ""
    state: str = "closed"
    max_issues: int = Field(default=20, ge=1, le=100)


class IngestResult(BaseModel):
    ingested: int
    skipped: int
    message: str


@router.post("/external/github-issues", response_model=IngestResult)
async def ingest_github_issues(
    req: GithubIssuesRequest, db: AsyncSession = Depends(get_db)
) -> IngestResult:
    await _ensure_project(db, req.project_key)

    src_row = await db.execute(
        select(KbExternalSource).where(
            KbExternalSource.source_type == "github_issues",
            KbExternalSource.source_url == req.repo,
        )
    )
    src = src_row.scalar_one_or_none()
    if not src:
        src = KbExternalSource(
            source_type="github_issues",
            source_url=req.repo,
            project_key=req.project_key,
        )
        db.add(src)
        await db.flush()

    url = f"https://api.github.com/repos/{req.repo}/issues"
    params: dict = {
        "state": req.state,
        "per_page": str(req.max_issues),
        "sort": "updated",
        "direction": "desc",
    }
    if req.labels:
        params["labels"] = req.labels

    headers = {"Accept": "application/vnd.github+json"}

    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"GitHub API error: {resp.status_code} {resp.text[:200]}",
            )
        issues = resp.json()

    ingested = 0
    skipped = 0

    for issue in issues:
        if issue.get("pull_request"):
            skipped += 1
            continue

        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        labels = [lb["name"] for lb in issue.get("labels", [])]
        issue_num = issue.get("number", 0)

        content = f"[GitHub Issue #{issue_num}] {title}\n\n{body[:2000]}"
        tags = ["external", "github-issue", req.repo.split("/")[-1]] + labels[:5]
        meta = {
            "source": "github",
            "repo": req.repo,
            "issue_number": issue_num,
            "state": issue.get("state"),
            "url": issue.get("html_url"),
        }

        chunk = await _store_external_chunk(
            db, req.project_key, content, "github_issue", tags, meta, src.id
        )
        if chunk:
            ingested += 1
        else:
            skipped += 1

    src.last_synced_at = datetime.now(UTC)
    src.sync_count += 1
    await db.commit()

    return IngestResult(
        ingested=ingested,
        skipped=skipped,
        message=f"{req.repo}から{ingested}件取り込み、{skipped}件スキップ",
    )


class StackOverflowRequest(BaseModel):
    query: str
    project_key: str
    tagged: str = ""
    max_results: int = Field(default=10, ge=1, le=30)


@router.post("/external/stackoverflow", response_model=IngestResult)
async def ingest_stackoverflow(
    req: StackOverflowRequest, db: AsyncSession = Depends(get_db)
) -> IngestResult:
    await _ensure_project(db, req.project_key)

    src_row = await db.execute(
        select(KbExternalSource).where(
            KbExternalSource.source_type == "stackoverflow",
            KbExternalSource.source_url == f"so:{req.query}",
        )
    )
    src = src_row.scalar_one_or_none()
    if not src:
        src = KbExternalSource(
            source_type="stackoverflow",
            source_url=f"so:{req.query}",
            project_key=req.project_key,
        )
        db.add(src)
        await db.flush()

    url = "https://api.stackexchange.com/2.3/search/advanced"
    params = {
        "order": "desc",
        "sort": "votes",
        "q": req.query,
        "site": "stackoverflow",
        "pagesize": str(req.max_results),
        "filter": "withbody",
        "accepted": "True",
    }
    if req.tagged:
        params["tagged"] = req.tagged

    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"SO API error: {resp.status_code}",
            )
        data = resp.json()

    ingested = 0
    skipped = 0

    for item in data.get("items", []):
        title = item.get("title", "")
        body = item.get("body", "") or ""
        clean_body = re.sub(r"<[^>]+>", "", body)[:2000]
        so_tags = item.get("tags", [])[:5]

        content = f"[StackOverflow] {title}\n\n{clean_body}"
        tags = ["external", "stackoverflow"] + so_tags
        meta = {
            "source": "stackoverflow",
            "question_id": item.get("question_id"),
            "score": item.get("score", 0),
            "answer_count": item.get("answer_count", 0),
            "url": item.get("link"),
        }

        chunk = await _store_external_chunk(
            db, req.project_key, content, "stackoverflow", tags, meta, src.id
        )
        if chunk:
            ingested += 1
        else:
            skipped += 1

    src.last_synced_at = datetime.now(UTC)
    src.sync_count += 1
    await db.commit()

    return IngestResult(
        ingested=ingested,
        skipped=skipped,
        message=f"Stack Overflowから{ingested}件取り込み、{skipped}件スキップ",
    )


class AuditRequest(BaseModel):
    project_key: str
    audit_type: str
    vulnerabilities: list[dict]


@router.post("/external/audit", response_model=IngestResult)
async def ingest_audit(
    req: AuditRequest, db: AsyncSession = Depends(get_db)
) -> IngestResult:
    if req.audit_type not in {"npm_audit", "pip_audit", "cargo_audit"}:
        raise HTTPException(
            status_code=400,
            detail="audit_type must be: npm_audit, pip_audit, cargo_audit",
        )

    await _ensure_project(db, req.project_key)

    src_row = await db.execute(
        select(KbExternalSource).where(
            KbExternalSource.source_type == req.audit_type,
            KbExternalSource.source_url == f"audit:{req.project_key}",
        )
    )
    src = src_row.scalar_one_or_none()
    if not src:
        src = KbExternalSource(
            source_type=req.audit_type,
            source_url=f"audit:{req.project_key}",
            project_key=req.project_key,
        )
        db.add(src)
        await db.flush()

    ingested = 0
    skipped = 0

    for vuln in req.vulnerabilities:
        name = vuln.get("name", "unknown")
        severity = vuln.get("severity", "unknown")
        title = vuln.get("title", "")
        description = vuln.get("description", "")
        url = vuln.get("url", "")

        content = (
            f"[脆弱性警告] {name} - {severity}\n"
            f"{title}\n{description[:1500]}\n"
            f"参照: {url}"
        )
        tags = ["external", "vulnerability", severity, req.audit_type]
        meta = {
            "source": req.audit_type,
            "package": name,
            "severity": severity,
            "advisory_url": url,
        }

        chunk = await _store_external_chunk(
            db, req.project_key, content, "vulnerability", tags, meta, src.id
        )
        if chunk:
            ingested += 1
        else:
            skipped += 1

    src.last_synced_at = datetime.now(UTC)
    src.sync_count += 1
    await db.commit()

    return IngestResult(
        ingested=ingested,
        skipped=skipped,
        message=f"{req.audit_type}: {ingested}件取り込み、{skipped}件スキップ",
    )
