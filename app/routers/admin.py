"""HO-83 Phase1: 管理用バルクインポート EP（代替案 / §C-1.5 案A）.

Notion 3DB（学び/開発ログ/失敗事例）を任意フィールドで knowhow へ投入する管理 API。
移行スクリプト（scripts/migrate_notion_to_knowhow.py・主案）の代替で、サーバ側で
変換＋embedding生成＋INSERT を行う。再移行や他DB取込が今後あるなら監査・冪等を API 層に
集約できる利点がある（仕様 §C-1.5）。

認証（神谷方針）:
  既存 KB_API_KEY とは **別系統** の専用キー ADMIN_IMPORT_KEY を X-Admin-Key ヘッダで検証する。
  ADMIN_IMPORT_KEY が **未設定なら 503**（誤って全開放しない＝安全側に倒す）。
  比較はタイミング攻撃回避のため hmac.compare_digest（app/auth.py と同方針）。

冪等: meta->>'notion_*_id' で重複スキップ。1リクエスト最大件数を制限。dry_run 対応。

⚠️ main.py への組込みは本ファイルでは行わない。差分は _ho83_migration/admin_wiring.patch.md 参照。
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.embedding import create_embedding
from app.models import KbChunk, KbIssue, KbProject, KbSession

router = APIRouter(tags=["admin"])

MAX_ITEMS_PER_REQUEST = 500

# kb_issues.reason の enum 許容値（db/schema.sql:183 / feedback.py:144）。
# thinking_mistake / verification_skip は無い → incomplete へ寄せ、原文は meta.failure_type に保持。
REASON_ENUM = {"stale", "wrong", "env_mismatch", "incomplete"}
DEFAULT_REASON = "incomplete"

CHUNK_TYPE = {
    "ルール (Must)": ("rule", 10), "憲法": ("rule", 10),
    "教訓 (Should)": ("insight", 6), "ベストプラクティス": ("insight", 6),
    "反パターン": ("anti_pattern", 7), "推測・仮説": ("insight", 3),
    "社長の思想": ("rule", 8),
}
SEED_AB = {"rule": (9.0, 1.0), "insight": (6.0, 2.0), "anti_pattern": (5.0, 2.0)}


async def require_admin_key(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> None:
    """ADMIN_IMPORT_KEY 未設定なら 503（安全側）。設定時は X-Admin-Key 一致を要求。"""
    expected = settings.admin_import_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin import disabled (ADMIN_IMPORT_KEY not configured)",
        )
    if not x_admin_key or not hmac.compare_digest(x_admin_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin key",
            headers={"WWW-Authenticate": "AdminKey"},
        )


class ImportRequest(BaseModel):
    kind: str = Field(description="learning | devlog | failure")
    items: list[dict] = Field(default_factory=list)
    project_key: str = "cto-lab"
    dry_run: bool = False


class ImportResultItem(BaseModel):
    index: int
    status: str  # imported | skipped | error
    chunk_id: int | None = None
    session_id: int | None = None
    issue_id: int | None = None
    detail: str | None = None


class ImportResponse(BaseModel):
    kind: str
    total_submitted: int
    total_imported: int
    total_skipped: int
    dry_run: bool
    results: list[ImportResultItem]


def _seed_ab(chunk_type: str) -> tuple[float, float]:
    return SEED_AB.get(chunk_type, (2.0, 2.0))


async def _chunk_meta_exists(db: AsyncSession, key: str, value: str) -> bool:
    row = await db.execute(
        text("SELECT 1 FROM kb_chunks WHERE meta->>:k = :v LIMIT 1").bindparams(k=key, v=value)
    )
    return row.first() is not None


async def _session_meta_exists(db: AsyncSession, key: str, value: str) -> bool:
    row = await db.execute(
        text("SELECT 1 FROM kb_sessions WHERE meta->>:k = :v LIMIT 1").bindparams(k=key, v=value)
    )
    return row.first() is not None


async def _ensure_project(db: AsyncSession, project_key: str) -> None:
    existing = await db.execute(select(KbProject).where(KbProject.project_key == project_key))
    if existing.scalar_one_or_none() is None:
        db.add(KbProject(project_key=project_key, display_name=project_key))
        await db.flush()


async def _embed(text_value: str, dry_run: bool):
    if dry_run or not text_value:
        return None
    try:
        return await create_embedding(text_value)
    except Exception:  # noqa: BLE001 — embedding 失敗は NULL 保存で握り（既存 ingest.py と同方針）
        return None


@router.post("/admin/import", response_model=ImportResponse, dependencies=[Depends(require_admin_key)])
async def admin_import(req: ImportRequest, db: AsyncSession = Depends(get_db)) -> ImportResponse:
    if req.kind not in {"learning", "devlog", "failure"}:
        raise HTTPException(status_code=400, detail="kind must be: learning | devlog | failure")
    if len(req.items) > MAX_ITEMS_PER_REQUEST:
        raise HTTPException(
            status_code=413,
            detail=f"Too many items ({len(req.items)} > {MAX_ITEMS_PER_REQUEST})",
        )

    results: list[ImportResultItem] = []
    imported = 0
    skipped = 0

    await _ensure_project(db, req.project_key)

    for idx, item in enumerate(req.items):
        try:
            # 各 item を SAVEPOINT で包む。1件の flush 失敗が後続を巻き込んで
            # トランザクション全体を汚染する（PendingRollbackError）のを防ぐ。
            async with db.begin_nested():
                if req.kind == "learning":
                    r = await _import_learning(db, req.project_key, item, req.dry_run)
                elif req.kind == "devlog":
                    r = await _import_devlog(db, req.project_key, item, req.dry_run)
                else:
                    r = await _import_failure(db, req.project_key, item, req.dry_run)
        except Exception as e:  # noqa: BLE001 — 失敗 item は savepoint ロールバックして続行
            r = ImportResultItem(index=idx, status="error", detail=str(e))
        r.index = idx
        results.append(r)
        if r.status == "imported":
            imported += 1
        elif r.status == "skipped":
            skipped += 1

    if req.dry_run:
        await db.rollback()
    else:
        await db.commit()

    return ImportResponse(
        kind=req.kind,
        total_submitted=len(req.items),
        total_imported=imported,
        total_skipped=skipped,
        dry_run=req.dry_run,
        results=results,
    )


async def _import_learning(
    db: AsyncSession, project_key: str, item: dict, dry_run: bool
) -> ImportResultItem:
    notion_id = item.get("notion_learning_id") or item.get("学びID") or item.get("id")
    if notion_id and await _chunk_meta_exists(db, "notion_learning_id", str(notion_id)):
        return ImportResultItem(index=0, status="skipped", detail="duplicate notion_learning_id")

    kind = item.get("種別") or item.get("kind") or ""
    chunk_type, imp = CHUNK_TYPE.get(str(kind).strip(), ("insight", 5))
    alpha = item.get("alpha値") or item.get("alpha")
    beta = item.get("beta値") or item.get("beta")
    if alpha is None or beta is None:
        s_a, s_b = _seed_ab(chunk_type)
        alpha = float(alpha) if alpha is not None else s_a
        beta = float(beta) if beta is not None else s_b
    else:
        alpha, beta = float(alpha), float(beta)
    confidence = float(alpha / (alpha + beta)) if (alpha + beta) > 0 else 0.5

    title = item.get("タイトル") or item.get("title") or ""
    body = item.get("内容") or item.get("body") or ""
    content = f"{title}\n{body}".strip()

    chunk = KbChunk(
        project_key=project_key,
        source_type="learning",
        source_id=int(notion_id) if str(notion_id or "").isdigit() else 0,
        chunk_type=chunk_type,
        content=content,
        importance_score=imp,
        tags=_as_list(item.get("カテゴリ") or item.get("tags")),
        meta={
            "notion_learning_id": str(notion_id) if notion_id is not None else None,
            "notion_url": item.get("url"),
            "status": item.get("状態"),
            "source_basis": item.get("根拠"),
            "applicable_condition": item.get("適用条件"),
        },
        alpha=alpha,
        beta=beta,
        confidence_score=confidence,
        recall_count=int(item.get("参照回数") or 0),
        is_deprecated=bool(item.get("is_deprecated")),
    )
    if not dry_run:
        emb = await _embed(content, dry_run)
        if emb is not None:
            chunk.embedding = emb
            chunk.embedding_model = settings.embedding_model
            chunk.embedding_dimensions = settings.embedding_dim
        db.add(chunk)
        await db.flush()
    return ImportResultItem(
        index=0, status="imported", chunk_id=getattr(chunk, "id", None)
    )


async def _import_devlog(
    db: AsyncSession, project_key: str, item: dict, dry_run: bool
) -> ImportResultItem:
    notion_id = item.get("notion_log_id") or item.get("依頼ID") or item.get("id")
    if notion_id and await _session_meta_exists(db, "notion_log_id", str(notion_id)):
        return ImportResultItem(index=0, status="skipped", detail="duplicate notion_log_id")

    request = item.get("依頼内容") or item.get("request") or ""
    result = item.get("結果サマリ") or item.get("result_summary") or ""
    learning = item.get("学び") or item.get("learning") or ""
    raw_log = "\n\n".join(p for p in [request, result, learning] if p).strip() or "(empty)"
    log_hash = hashlib.sha256(f"{request}{notion_id or ''}".encode("utf-8")).hexdigest()

    session = KbSession(
        project_key=project_key,
        tool=str(item.get("実行ツール") or item.get("tool") or "devin").strip().lower().replace(" ", "_"),
        status=item.get("status") or "partial",
        environment=item.get("environment") or "local",
        duration_seconds=(int(item["所要時間（分）"]) * 60) if item.get("所要時間（分）") else None,
        raw_log=raw_log,
        normalized_log=raw_log.strip(),
        summary_text=result or None,
        tags=_as_list(item.get("対象システム")),
        error_count=int(item.get("エラー回数") or 0),
        retry_count=int(item.get("リトライ回数") or 0),
        ingest_state="summarized",
        hash=log_hash,
        meta={"notion_log_id": str(notion_id) if notion_id is not None else None,
              "feedback_helpful": item.get("フィードバック有用度")},
    )
    if dry_run:
        return ImportResultItem(index=0, status="imported")

    db.add(session)
    await db.flush()
    summary_content = "\n\n".join(p for p in [result, learning] if p).strip() or raw_log
    s_a, s_b = SEED_AB["insight"]
    chunk = KbChunk(
        project_key=project_key,
        source_type="session",
        source_id=session.id,
        chunk_type="summary",
        content=summary_content,
        importance_score=5,
        tags=_as_list(item.get("対象システム")),
        meta={"notion_log_id": str(notion_id) if notion_id is not None else None},
        alpha=s_a,
        beta=s_b,
        confidence_score=float(s_a / (s_a + s_b)),
    )
    emb = await _embed(summary_content, dry_run)
    if emb is not None:
        chunk.embedding = emb
        chunk.embedding_model = settings.embedding_model
        chunk.embedding_dimensions = settings.embedding_dim
    db.add(chunk)
    await db.flush()
    return ImportResultItem(index=0, status="imported", session_id=session.id, chunk_id=chunk.id)


async def _import_failure(
    db: AsyncSession, project_key: str, item: dict, dry_run: bool
) -> ImportResultItem:
    notion_id = item.get("notion_failure_id") or item.get("失敗ID") or item.get("id")
    if notion_id and await _chunk_meta_exists(db, "notion_failure_id", str(notion_id)):
        return ImportResultItem(index=0, status="skipped", detail="duplicate notion_failure_id")

    parts = [
        item.get("タイトル") or "",
        f"何が起きたか: {item.get('何が起きたか')}" if item.get("何が起きたか") else "",
        f"原因: {item.get('原因')}" if item.get("原因") else "",
        f"対策: {item.get('対策')}" if item.get("対策") else "",
        f"教訓: {item.get('教訓')}" if item.get("教訓") else "",
    ]
    content = "\n\n".join(p for p in parts if p).strip() or "(empty)"

    failure_type = item.get("失敗類型") or item.get("failure_type")
    reason = str(failure_type) if failure_type in REASON_ENUM else DEFAULT_REASON
    severity = item.get("重大度")
    imp = {"🔴 重大": 8, "🟡 警告": 6, "🔵 軽微": 4}.get(str(severity).strip() if severity else "", 6)

    is_done = bool(item.get("対策完了"))
    a, b = SEED_AB["anti_pattern"]

    chunk = KbChunk(
        project_key=project_key,
        source_type="session",
        source_id=0,
        chunk_type="anti_pattern",
        content=content,
        importance_score=imp,
        tags=_as_list(item.get("タグ")),
        meta={
            "notion_failure_id": str(notion_id) if notion_id is not None else None,
            "failure_type": failure_type,  # 原文保持（enum外の値もここに残す）
            "severity": severity,
            "environment": item.get("環境"),
            "recurrence_count": int(item.get("再発回数") or 0),
        },
        alpha=a,
        beta=b,
        confidence_score=float(a / (a + b)),
    )
    if dry_run:
        return ImportResultItem(index=0, status="imported")

    emb = await _embed(content, dry_run)
    if emb is not None:
        chunk.embedding = emb
        chunk.embedding_model = settings.embedding_model
        chunk.embedding_dimensions = settings.embedding_dim
    db.add(chunk)
    await db.flush()
    issue = KbIssue(
        project_key=project_key,
        chunk_id=chunk.id,
        reason=reason,
        status="closed" if is_done else "open",
    )
    db.add(issue)
    await db.flush()
    return ImportResultItem(index=0, status="imported", chunk_id=chunk.id, issue_id=issue.id)


def _as_list(v) -> list[str]:
    if v in (None, ""):
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]
