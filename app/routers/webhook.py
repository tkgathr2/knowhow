"""GitHub Webhook endpoint for automatic knowledge ingestion.

Receives GitHub webhook events and automatically memorizes:
- PR merged: title, description, changed files
- Issue closed: title, body, labels
- Push: commit messages
"""

import hashlib
import hmac
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.embedding import create_embedding
from app.models import KbChunk, KbExternalSource, KbProject

router = APIRouter(tags=["webhook"])
_logger = logging.getLogger(__name__)


def _verify_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not secret:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def _repo_to_project_key(repo_full_name: str) -> str:
    """Convert 'owner/repo' to project_key (repo name only)."""
    return repo_full_name.split("/")[-1]


async def _ensure_project(db: AsyncSession, project_key: str) -> None:
    row = await db.execute(
        select(KbProject).where(KbProject.project_key == project_key)
    )
    if not row.scalar_one_or_none():
        db.add(KbProject(project_key=project_key, display_name=project_key))
        await db.flush()


async def _get_or_create_source(
    db: AsyncSession, source_type: str, source_url: str, project_key: str
) -> KbExternalSource:
    row = await db.execute(
        select(KbExternalSource).where(
            KbExternalSource.source_type == source_type,
            KbExternalSource.source_url == source_url,
        )
    )
    src = row.scalar_one_or_none()
    if not src:
        src = KbExternalSource(
            source_type=source_type,
            source_url=source_url,
            project_key=project_key,
        )
        db.add(src)
        await db.flush()
    return src


async def _store_chunk(
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
        source_type="webhook",
        source_id=source_id,
        chunk_type=chunk_type,
        content=content,
        importance_score=5,
        confidence_score=0.85,
        alpha=8.5,
        beta=1.5,
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


async def _handle_pr_merged(payload: dict, db: AsyncSession) -> dict:
    """Handle pull_request event with action=closed and merged=true."""
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "")
    project_key = _repo_to_project_key(repo_full_name)

    pr_number = pr.get("number", 0)
    title = pr.get("title", "")
    body = (pr.get("body") or "")[:3000]
    merged_by = pr.get("merged_by", {}).get("login", "unknown")
    base_branch = pr.get("base", {}).get("ref", "")
    head_branch = pr.get("head", {}).get("ref", "")
    labels = [lb["name"] for lb in pr.get("labels", [])]
    changed_files = pr.get("changed_files", 0)
    additions = pr.get("additions", 0)
    deletions = pr.get("deletions", 0)
    html_url = pr.get("html_url", "")

    content = (
        f"[PR #{pr_number} merged] {title}\n"
        f"ブランチ: {head_branch} -> {base_branch}\n"
        f"マージ者: {merged_by}\n"
        f"変更: {changed_files}ファイル (+{additions} -{deletions})\n"
        f"URL: {html_url}\n\n"
        f"{body}"
    )

    tags = ["webhook", "github-pr", "merged", project_key] + labels[:5]
    meta = {
        "source": "github_webhook",
        "event": "pull_request.merged",
        "repo": repo_full_name,
        "pr_number": pr_number,
        "merged_by": merged_by,
        "base_branch": base_branch,
        "head_branch": head_branch,
        "changed_files": changed_files,
        "additions": additions,
        "deletions": deletions,
        "url": html_url,
    }

    await _ensure_project(db, project_key)
    src = await _get_or_create_source(db, "github_webhook", repo_full_name, project_key)

    chunk = await _store_chunk(db, project_key, content, "pr_merged", tags, meta, src.id)

    src.last_synced_at = datetime.now(UTC)
    src.sync_count += 1
    await db.commit()

    if chunk:
        _logger.info("PR #%d merged -> memorized (chunk_id=%d)", pr_number, chunk.id)
        return {"status": "ingested", "event": "pr_merged", "pr_number": pr_number, "chunk_id": chunk.id}
    return {"status": "skipped", "event": "pr_merged", "pr_number": pr_number, "reason": "duplicate"}


async def _handle_issue_closed(payload: dict, db: AsyncSession) -> dict:
    """Handle issues event with action=closed."""
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "")
    project_key = _repo_to_project_key(repo_full_name)

    issue_number = issue.get("number", 0)
    title = issue.get("title", "")
    body = (issue.get("body") or "")[:3000]
    labels = [lb["name"] for lb in issue.get("labels", [])]
    html_url = issue.get("html_url", "")
    closed_by = issue.get("closed_by", {})
    closer = closed_by.get("login", "unknown") if closed_by else "unknown"

    content = (
        f"[Issue #{issue_number} closed] {title}\n"
        f"クローズ者: {closer}\n"
        f"ラベル: {', '.join(labels)}\n"
        f"URL: {html_url}\n\n"
        f"{body}"
    )

    tags = ["webhook", "github-issue", "closed", project_key] + labels[:5]
    meta = {
        "source": "github_webhook",
        "event": "issues.closed",
        "repo": repo_full_name,
        "issue_number": issue_number,
        "closed_by": closer,
        "url": html_url,
    }

    await _ensure_project(db, project_key)
    src = await _get_or_create_source(db, "github_webhook", repo_full_name, project_key)

    chunk = await _store_chunk(db, project_key, content, "issue_closed", tags, meta, src.id)

    src.last_synced_at = datetime.now(UTC)
    src.sync_count += 1
    await db.commit()

    if chunk:
        _logger.info("Issue #%d closed -> memorized (chunk_id=%d)", issue_number, chunk.id)
        return {"status": "ingested", "event": "issue_closed", "issue_number": issue_number, "chunk_id": chunk.id}
    return {"status": "skipped", "event": "issue_closed", "issue_number": issue_number, "reason": "duplicate"}


async def _handle_push(payload: dict, db: AsyncSession) -> dict:
    """Handle push event - memorize commit messages."""
    repo = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "")
    project_key = _repo_to_project_key(repo_full_name)
    ref = payload.get("ref", "")
    branch = ref.replace("refs/heads/", "")
    commits = payload.get("commits", [])
    pusher = payload.get("pusher", {}).get("name", "unknown")

    if not commits:
        return {"status": "skipped", "event": "push", "reason": "no_commits"}

    commit_lines = []
    for c in commits[:20]:
        sha_short = c.get("id", "")[:7]
        msg = c.get("message", "").split("\n")[0][:200]
        author = c.get("author", {}).get("name", "unknown")
        commit_lines.append(f"  {sha_short} ({author}): {msg}")

    content = (
        f"[Push to {branch}] {len(commits)}コミット by {pusher}\n"
        f"リポジトリ: {repo_full_name}\n\n"
        + "\n".join(commit_lines)
    )

    tags = ["webhook", "github-push", project_key, branch]
    meta = {
        "source": "github_webhook",
        "event": "push",
        "repo": repo_full_name,
        "branch": branch,
        "pusher": pusher,
        "commit_count": len(commits),
        "head_commit": payload.get("head_commit", {}).get("id", ""),
    }

    await _ensure_project(db, project_key)
    src = await _get_or_create_source(db, "github_webhook", repo_full_name, project_key)

    chunk = await _store_chunk(db, project_key, content, "push", tags, meta, src.id)

    src.last_synced_at = datetime.now(UTC)
    src.sync_count += 1
    await db.commit()

    if chunk:
        _logger.info("Push to %s -> memorized (chunk_id=%d)", branch, chunk.id)
        return {"status": "ingested", "event": "push", "branch": branch, "chunk_id": chunk.id}
    return {"status": "skipped", "event": "push", "branch": branch, "reason": "duplicate"}


class WebhookResponse(BaseModel):
    status: str
    event: str | None = None
    message: str = ""
    detail: dict = {}


@router.post("/webhook/github", response_model=WebhookResponse)
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    db: AsyncSession = Depends(get_db),
) -> WebhookResponse:
    """Receive GitHub webhook events and auto-memorize to knowledge DB."""
    body = await request.body()

    if not _verify_signature(body, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    event = x_github_event or ""

    _logger.info("Webhook received: event=%s", event)

    if event == "ping":
        return WebhookResponse(
            status="ok",
            event="ping",
            message="pong",
            detail={"zen": payload.get("zen", "")},
        )

    if event == "pull_request":
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        if action == "closed" and pr.get("merged"):
            result = await _handle_pr_merged(payload, db)
            return WebhookResponse(
                status=result["status"],
                event="pull_request.merged",
                message=f"PR #{result.get('pr_number')} processed",
                detail=result,
            )
        return WebhookResponse(
            status="ignored",
            event=f"pull_request.{action}",
            message=f"PR action '{action}' is not tracked",
        )

    if event == "issues":
        action = payload.get("action", "")
        if action == "closed":
            result = await _handle_issue_closed(payload, db)
            return WebhookResponse(
                status=result["status"],
                event="issues.closed",
                message=f"Issue #{result.get('issue_number')} processed",
                detail=result,
            )
        return WebhookResponse(
            status="ignored",
            event=f"issues.{action}",
            message=f"Issue action '{action}' is not tracked",
        )

    if event == "push":
        ref = payload.get("ref", "")
        default_branch = payload.get("repository", {}).get("default_branch", "main")
        branch = ref.replace("refs/heads/", "")
        if branch == default_branch:
            result = await _handle_push(payload, db)
            return WebhookResponse(
                status=result["status"],
                event="push",
                message=f"Push to {branch} processed",
                detail=result,
            )
        return WebhookResponse(
            status="ignored",
            event="push",
            message=f"Push to non-default branch '{branch}' ignored",
        )

    return WebhookResponse(
        status="ignored",
        event=event,
        message=f"Event '{event}' is not tracked",
    )
