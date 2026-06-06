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

# 開発ログDB ステータス -> kb_sessions.status
STATUS_MAP = {
    "完了": "success", "success": "success",
    "失敗": "fail", "fail": "fail",
    "社長確認待ち": "partial", "進行中": "partial", "受付": "partial", "保留": "partial",
}
# 開発ログDB 実行ツール -> kb_sessions.tool（括弧前トークンで正規化）
TOOL_MAP = {
    "claude code": "claude_code", "claude_code": "claude_code",
    "cowork": "cowork", "devin": "devin", "cursor": "cursor",
    "chatgpt": "chatgpt", "手動": "manual",
}
# 学びDB 状態 -> is_deprecated（非推奨/凍結なら True）
DEPRECATED_STATES = {"🔴 非推奨", "🔴非推奨", "非推奨", "❄ 凍結", "❄凍結", "凍結"}


def _norm_token(v) -> str:
    """select 表示値から先頭トークンを取り出す。'env_mismatch (環境差異)' -> 'env_mismatch'。"""
    if v in (None, ""):
        return ""
    s = str(v)
    for sep in (" (", "（", "("):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    return s.strip()


def _map_tool(v) -> str:
    if v in (None, ""):
        return "devin"
    key = _norm_token(v).lower()
    return TOOL_MAP.get(key, key.replace(" ", "_") or "devin")


def _map_status(v) -> str:
    if v in (None, ""):
        return "partial"
    return STATUS_MAP.get(str(v).strip(), "partial")


def _map_reason(failure_type) -> str:
    """失敗類型 -> kb_issues.reason enum。enum外（thinking_mistake等）は incomplete に寄せる。"""
    tok = _norm_token(failure_type).lower()
    return tok if tok in REASON_ENUM else DEFAULT_REASON


def _sev_imp(severity) -> int:
    s = str(severity or "")
    if "重大" in s:
        return 8
    if "警告" in s:
        return 6
    if "軽微" in s:
        return 4
    return 6


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


async def _session_hash_exists(db: AsyncSession, project_key: str, log_hash: str) -> bool:
    # kb_sessions に meta 列は無い。冪等は UNIQUE(project_key, hash) を使う。
    row = await db.execute(
        text("SELECT 1 FROM kb_sessions WHERE project_key = :p AND hash = :h LIMIT 1")
        .bindparams(p=project_key, h=log_hash)
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
    # 重要(分析§7): Notion 学びの α/β は全件が初期値(1/1, 一部2/1)のままで、
    # そのまま移送すると confidence≈0.50 で 97/98 件が閾値0.70割れ＝検索に出ない事故になる。
    # よって種別ベースのシードで必ず上書きする（元値は meta.notion_alpha/beta に保持して非破壊）。
    notion_alpha = item.get("alpha値") if item.get("alpha値") is not None else item.get("alpha")
    notion_beta = item.get("beta値") if item.get("beta値") is not None else item.get("beta")
    alpha, beta = _seed_ab(chunk_type)
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
            "notion_alpha": notion_alpha,
            "notion_beta": notion_beta,
        },
        alpha=alpha,
        beta=beta,
        confidence_score=confidence,
        recall_count=int(item.get("参照回数") or 0),
        is_deprecated=bool(item.get("is_deprecated")) or (str(item.get("状態") or "").strip() in DEPRECATED_STATES),
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
    request = item.get("依頼内容") or item.get("request") or ""
    result = item.get("結果サマリ") or item.get("result_summary") or ""
    learning = item.get("学び") or item.get("learning") or ""
    raw_log = "\n\n".join(p for p in [request, result, learning] if p).strip() or "(empty)"
    log_hash = hashlib.sha256(f"{request}{notion_id or ''}".encode("utf-8")).hexdigest()
    if await _session_hash_exists(db, project_key, log_hash):
        return ImportResultItem(index=0, status="skipped", detail="duplicate session hash")

    session = KbSession(
        project_key=project_key,
        tool=_map_tool(item.get("実行ツール") or item.get("tool")),
        status=_map_status(item.get("ステータス") or item.get("status")),
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
        summary_json={"notion_log_id": str(notion_id) if notion_id is not None else None,
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
    reason = _map_reason(failure_type)  # 'env_mismatch (環境差異)' 等を括弧前で正規化。enum外は incomplete。
    severity = item.get("重大度")
    imp = _sev_imp(severity)

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
