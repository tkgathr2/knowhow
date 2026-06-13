"""1日のダイジェストAPI。

GET  /api/digest/daily?days=N … 保存済みダイジェストを返す（足りない日はその場で生成して保存）
POST /api/digest/run          … 指定日を強制再生成（X-API-Key 保護）

生成はLLM（gpt-4o-mini・既存 openai_api_key 流用）。未設定/失敗時はルールベースで
必ず文章を返す（壊れて空白になるくらいなら素朴な文を出す）。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import digest_logic
from app.auth import require_api_key
from app.config import settings
from app.database import get_db
from app.models import KbChunk

router = APIRouter(tags=["digest"])
_logger = logging.getLogger(__name__)

_LOG_SOURCE = "webhook"
_LLM_MODEL = "gpt-4o-mini"
_MAX_LLM_PER_REQUEST = 3      # 1リクエストでLLM生成する日数の上限（応答速度の安全弁）
_TODAY_REFRESH_MIN = 30       # 当日分（暫定）を作り直す間隔


class DigestEntry(BaseModel):
    date: str
    headline: str
    body: str
    model: str
    is_final: bool
    stats: dict


class DigestResponse(BaseModel):
    days: int
    entries: list[DigestEntry]


class RunRequest(BaseModel):
    date: str | None = None  # YYYY-MM-DD（省略時は昨日UTC）


class RunResponse(BaseModel):
    date: str
    headline: str
    model: str


async def _day_data(db: AsyncSession, d: date) -> tuple[dict, list[dict]]:
    """その日（UTC）の統計と「増えた学び」一覧を取る。"""
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    in_day = [KbChunk.created_at >= start, KbChunk.created_at < end]

    async def _count(*extra) -> int:
        return int(
            (await db.execute(select(func.count(KbChunk.id)).where(*in_day, *extra))).scalar() or 0
        )

    asset_added = await _count(KbChunk.source_type != _LOG_SOURCE)
    log_added = await _count(KbChunk.source_type == _LOG_SOURCE)
    deprecated = await _count(KbChunk.is_deprecated.is_(True))
    recalled = int(
        (
            await db.execute(
                select(func.count(KbChunk.id)).where(
                    KbChunk.last_recalled_at.isnot(None),
                    KbChunk.last_recalled_at >= start,
                    KbChunk.last_recalled_at < end,
                )
            )
        ).scalar()
        or 0
    )

    # SQL側で文字列を切らない（left()はバイト切りで22021を起こす・dashboard参照）
    rows = await db.execute(
        select(KbChunk.project_key, KbChunk.tags, KbChunk.content)
        .where(*in_day, KbChunk.source_type != _LOG_SOURCE)
        .order_by(KbChunk.created_at.desc())
        .limit(200)
    )
    items = [
        {"project_key": r.project_key, "tags": r.tags or [], "content": (r.content or "")[:200]}
        for r in rows
    ]
    # 前日比・累計（本文に手応えを織り込むため）。累計＝その日の終わりまでの正味ナレッジ。
    cumulative = int(
        (
            await db.execute(
                select(func.count(KbChunk.id)).where(
                    KbChunk.created_at < end, KbChunk.source_type != _LOG_SOURCE
                )
            )
        ).scalar()
        or 0
    )
    prev_cumulative = cumulative - asset_added
    growth_pct = (
        round(asset_added / prev_cumulative * 100, 1) if prev_cumulative > 0 else None
    )
    stats = {
        "asset_added": asset_added,
        "log_added": log_added,
        "deprecated": deprecated,
        "recalled": recalled,
        "asset_cumulative": cumulative,
        "growth_pct": growth_pct,
    }
    return stats, items


async def _llm_digest(d: str, stats: dict, items: list[dict]) -> tuple[dict | None, str]:
    """LLMでダイジェスト生成。(結果dict or None, 使ったモデル名)。"""
    if not settings.openai_api_key:
        return None, ""
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.chat.completions.create(
            model=_LLM_MODEL,
            messages=[
                {"role": "system", "content": digest_logic.SYSTEM_PROMPT},
                {"role": "user", "content": digest_logic.build_llm_input(d, stats, items)},
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=1100,
        )
        raw = json.loads(resp.choices[0].message.content or "{}")
        return raw, _LLM_MODEL
    except Exception as e:  # LLM失敗は致命にしない（フォールバックへ）
        _logger.warning("digest LLM failed for %s: %s", d, e)
        return None, ""


async def _upsert(db: AsyncSession, d: date, digest: dict, stats: dict, model: str, is_final: bool) -> None:
    await db.execute(
        text(
            """
            INSERT INTO kb_daily_digest (digest_date, headline, body, stats, model, is_final, updated_at)
            VALUES (:d, :h, :b, CAST(:s AS jsonb), :m, :f, now())
            ON CONFLICT (digest_date) DO UPDATE SET
              headline = EXCLUDED.headline, body = EXCLUDED.body, stats = EXCLUDED.stats,
              model = EXCLUDED.model, is_final = EXCLUDED.is_final, updated_at = now()
            """
        ),
        {"d": d, "h": digest["headline"], "b": digest["body"],
         "s": json.dumps(stats, ensure_ascii=False), "m": model, "f": is_final},
    )
    await db.commit()


async def _generate(db: AsyncSession, d: date, use_llm: bool) -> dict:
    """1日分を生成して保存し、エントリ dict を返す。"""
    ds = d.isoformat()
    stats, items = await _day_data(db, d)
    raw, model = (await _llm_digest(ds, stats, items)) if use_llm else (None, "")
    digest = digest_logic.normalize_llm_digest(raw, ds, stats, items)
    if not model:
        model = "rules"
    is_final = d < datetime.now(timezone.utc).date()
    await _upsert(db, d, digest, stats, model, is_final)
    return {"date": ds, "headline": digest["headline"], "body": digest["body"],
            "model": model, "is_final": is_final, "stats": stats}


@router.get("/digest/daily", response_model=DigestResponse)
async def get_daily_digests(days: int = 14, db: AsyncSession = Depends(get_db)) -> DigestResponse:
    days = max(1, min(days, 60))
    today = datetime.now(timezone.utc).date()
    wanted = [today - timedelta(days=i) for i in range(days)]

    rows = await db.execute(
        text(
            "SELECT digest_date, headline, body, stats, model, is_final, updated_at "
            "FROM kb_daily_digest WHERE digest_date >= :since"
        ),
        {"since": wanted[-1]},
    )
    stored: dict[str, dict] = {}
    for r in rows:
        stored[r.digest_date.isoformat()] = {
            "date": r.digest_date.isoformat(),
            "headline": r.headline,
            "body": r.body,
            "model": r.model,
            "is_final": r.is_final,
            "stats": r.stats if isinstance(r.stats, dict) else json.loads(r.stats or "{}"),
            "_updated_at": r.updated_at,
        }

    # 生成が必要な日：未保存の日＋当日(暫定)が古い場合。LLMは新しい日から最大3日分。
    now = datetime.now(timezone.utc)
    to_generate: list[date] = []
    for d in wanted:  # wanted は新しい順
        ds = d.isoformat()
        if ds not in stored:
            to_generate.append(d)
        elif not stored[ds]["is_final"]:
            updated = stored[ds].get("_updated_at")
            if updated is None or (now - updated).total_seconds() > _TODAY_REFRESH_MIN * 60:
                to_generate.append(d)

    llm_budget = _MAX_LLM_PER_REQUEST
    for d in to_generate:
        use_llm = llm_budget > 0
        entry = await _generate(db, d, use_llm=use_llm)
        if entry["model"] != "rules":
            llm_budget -= 1
        stored[entry["date"]] = entry

    entries = [
        DigestEntry(**{k: v for k, v in stored[d.isoformat()].items() if not k.startswith("_")})
        for d in wanted
        if d.isoformat() in stored
    ]
    return DigestResponse(days=days, entries=entries)


@router.post("/digest/run", response_model=RunResponse, dependencies=[Depends(require_api_key)])
async def run_digest(req: RunRequest, db: AsyncSession = Depends(get_db)) -> RunResponse:
    if req.date:
        d = date.fromisoformat(req.date)
    else:
        d = datetime.now(timezone.utc).date() - timedelta(days=1)
    entry = await _generate(db, d, use_llm=True)
    return RunResponse(date=entry["date"], headline=entry["headline"], model=entry["model"])
