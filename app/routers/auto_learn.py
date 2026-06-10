"""学びの自動ingest受け口。

SessionEnd フックから「セッションの抜粋」を受け取り、サーバ側で OpenAI により
「教訓・型」へ蒸留＋PIIマスクし、knowhow に学びとして memorize する。
鍵はサーバの OPENAI_API_KEY を再利用するので、フック側は KB_API_KEY だけでよい。

- 認証: require_api_key（KB_API_KEY）。フックが X-API-Key で叩く。
- 蒸留して **要約のみ** を保存（生ログ/transcript は保存しない＝PII最小化）。
- 重複は要約のhashでスキップ。OpenAI未設定・蒸留失敗・空入力は stored=false で安全に返す。
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_key
from app.config import settings
from app.database import get_db
from app.embedding import create_embedding
from app.models import KbChunk, KbProject, KbSession

router = APIRouter(tags=["auto-learn"])

_DISTILL_MODEL = "gpt-4o-mini"
_MAX_INPUT_CHARS = 18000

_SYSTEM = (
    "あなたは開発の振り返り係です。渡された開発セッションの抜粋から、"
    "再利用できる『教訓・型・落とし穴・うまくいった手順』だけを日本語で簡潔に抽出します。"
    "厳守: (1)個人情報(氏名/メール/電話/住所/口座/トークンや鍵の値)は出力せずマスクする。"
    "(2)生ログのコピペでなく要点に蒸留(最大800字)。(3)開発上の学びが無ければ skip=true。"
    'JSONのみで出力: {"skip": false, "summary": "...", "tags": ["..."]}'
)


class AutoLearnRequest(BaseModel):
    project_key: str = "cto-lab"
    transcript: str
    tags: list[str] = Field(default_factory=list)


class AutoLearnResponse(BaseModel):
    stored: bool
    chunk_id: int | None = None
    reason: str | None = None


async def _distill(transcript: str) -> dict | None:
    if not settings.openai_api_key:
        return None
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    text = transcript[-_MAX_INPUT_CHARS:]
    try:
        resp = await client.chat.completions.create(
            model=_DISTILL_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=700,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return None


@router.post("/auto-learn", response_model=AutoLearnResponse, dependencies=[Depends(require_api_key)])
async def auto_learn(
    req: AutoLearnRequest, db: AsyncSession = Depends(get_db)
) -> AutoLearnResponse:
    if not req.transcript or len(req.transcript.strip()) < 400:
        return AutoLearnResponse(stored=False, reason="too_short")

    distilled = await _distill(req.transcript)
    if not distilled:
        return AutoLearnResponse(stored=False, reason="distill_unavailable")
    if distilled.get("skip") or not distilled.get("summary"):
        return AutoLearnResponse(stored=False, reason="no_lesson")

    summary = str(distilled["summary"]).strip()[:1500]
    tags = ["auto-ingest", "学び", *[str(t)[:40] for t in (distilled.get("tags") or [])]]
    tags = list(dict.fromkeys(tags + list(req.tags)))[:12]

    project = (
        await db.execute(select(KbProject).where(KbProject.project_key == req.project_key))
    ).scalar_one_or_none()
    if not project:
        db.add(KbProject(project_key=req.project_key, display_name=req.project_key))
        await db.flush()

    log_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    existing = (
        await db.execute(
            select(KbSession).where(
                KbSession.project_key == req.project_key, KbSession.hash == log_hash
            )
        )
    ).scalar_one_or_none()
    if existing:
        return AutoLearnResponse(stored=False, reason="duplicate")

    session = KbSession(
        project_key=req.project_key,
        tool="claude_code",
        status="success",
        environment="local",
        raw_log=summary,
        normalized_log=summary,
        tags=tags,
        hash=log_hash,
        ingest_state="summarized",
    )
    db.add(session)
    await db.flush()

    chunk = KbChunk(
        project_key=req.project_key,
        source_type="session",
        source_id=session.id,
        chunk_type="session_log",
        content=summary,
        importance_score=5,
        confidence_score=0.85,
        alpha=8.0,
        beta=1.0,
        tags=tags,
        meta={"source": "session-end-auto"},
    )
    db.add(chunk)
    try:
        embedding = await create_embedding(summary)
        if embedding is not None:
            chunk.embedding = embedding
            chunk.embedding_model = settings.embedding_model
            chunk.embedding_dimensions = settings.embedding_dim
            session.ingest_state = "embedded"
    except Exception:
        session.ingest_state = "failed_embedding"

    await db.commit()
    await db.refresh(chunk)
    return AutoLearnResponse(stored=True, chunk_id=chunk.id)
