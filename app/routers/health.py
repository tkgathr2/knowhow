"""ヘルスチェック。

2026-07-25 追加: embedding の実測。
これまで /health は常に {"status":"ok"} を返すだけだったため、OpenAI が
insufficient_quota (429) を返し続けて 1ヶ月半 embedding が付いていなかったのに
「緑」に見え続けた（嘘の緑）。/api/ingest も embedding 失敗時に HTTP 200 を返し、
recall は embedding が無いと黙って全文検索へフォールバックするため、
どこにも異常が出ないまま本来の意味検索だけが死んでいた。

そこで ?deep=1 のときだけ実際に embedding を1回生成し、その成否と
未embedding件数を返す。ここが false になったら本当に壊れている。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.embedding import create_embedding

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "knowhow"}


@router.get("/health/embedding")
async def health_embedding(db: AsyncSession = Depends(get_db)) -> dict:
    """embedding が本当に生成できるかを実測する（監視用）。

    ok=false なら意味検索が死んでいる。朝パトロールはここを見る。
    """
    provider = settings.active_embedding_provider
    result: dict = {
        "ok": False,
        "provider": provider,
        "keyConfigured": provider != "none",
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dim,
    }

    # 実際に1回叩く（キーの有無だけで緑にしない）
    try:
        vector = await create_embedding("health check")
        result["ok"] = vector is not None and len(vector) == settings.embedding_dim
        if vector is None:
            result["error"] = "create_embedding returned None (API key not configured)"
    except Exception as exc:  # noqa: BLE001 - 失敗理由をそのまま監視に出す
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]

    # 積み残し（指紋なしチャンク／embedding失敗セッション）の実数
    try:
        row = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FILTER (WHERE embedding IS NULL) AS missing, "
                    "COUNT(*) AS total FROM kb_chunks"
                )
            )
        ).one()
        result["chunksMissingEmbedding"] = int(row.missing)
        result["chunksTotal"] = int(row.total)
        failed = (
            await db.execute(
                text("SELECT COUNT(*) FROM kb_sessions WHERE ingest_state = 'failed_embedding'")
            )
        ).scalar_one()
        result["sessionsFailedEmbedding"] = int(failed)
    except Exception as exc:  # noqa: BLE001
        result["backlogError"] = f"{type(exc).__name__}: {exc}"[:200]

    return result
