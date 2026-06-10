"""コストカッターくん（token-cutter）実績 API。

POST /token-cutter/event : ゲート発動イベントを記録（認証なし開放＝各PCのフックが鍵なしで叩く）。
GET  /token-cutter/stats : 実績を集計（ダッシュボード用・閲覧保護下）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import token_cutter as tc
from app.database import get_db
from app.models import KbTokenCutterEvent

router = APIRouter(tags=["token-cutter"])


class TokenCutterEventIn(BaseModel):
    tool: str
    reason: str
    pc: str | None = None
    target_kb: int | None = None
    est_tokens: int = Field(default=0, ge=0)


class TokenCutterEventOut(BaseModel):
    ok: bool
    id: int


@router.post("/token-cutter/event", response_model=TokenCutterEventOut)
async def record_event(
    ev: TokenCutterEventIn, db: AsyncSession = Depends(get_db)
) -> TokenCutterEventOut:
    row = KbTokenCutterEvent(
        pc=(ev.pc or None),
        tool=ev.tool[:40],
        reason=ev.reason[:40],
        target_kb=ev.target_kb,
        est_tokens=max(0, int(ev.est_tokens or 0)),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return TokenCutterEventOut(ok=True, id=row.id)


class NameCount(BaseModel):
    name: str
    count: int
    est_tokens: int = 0


class DailyPoint(BaseModel):
    date: str
    events: int
    est_tokens: int


class RecentEvent(BaseModel):
    occurred_at: datetime
    pc: str | None
    tool: str
    reason: str
    target_kb: int | None
    est_tokens: int


class TokenCutterTotals(BaseModel):
    events: int
    est_tokens: int
    est_tokens_human: str
    pcs: int
    by_reason: list[NameCount]
    by_pc: list[NameCount]
    by_tool: list[NameCount]


class TokenCutterStats(BaseModel):
    days: int
    since: str
    totals: TokenCutterTotals
    daily: list[DailyPoint]
    recent: list[RecentEvent]


_DAY = func.to_char(func.date_trunc("day", KbTokenCutterEvent.occurred_at), "YYYY-MM-DD")


@router.get("/token-cutter/stats", response_model=TokenCutterStats)
async def get_stats(
    days: int = 30, db: AsyncSession = Depends(get_db)
) -> TokenCutterStats:
    days = max(1, min(days, 120))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    w = KbTokenCutterEvent.occurred_at >= since

    totals_row = (
        await db.execute(
            select(
                func.count(KbTokenCutterEvent.id),
                func.coalesce(func.sum(KbTokenCutterEvent.est_tokens), 0),
                func.count(func.distinct(KbTokenCutterEvent.pc)),
            ).where(w)
        )
    ).one()
    total_events, total_tokens, total_pcs = totals_row

    daily_rows = (
        await db.execute(
            select(
                _DAY,
                func.count(KbTokenCutterEvent.id),
                func.coalesce(func.sum(KbTokenCutterEvent.est_tokens), 0),
            ).where(w).group_by(_DAY)
        )
    ).all()
    events_by_day: dict[str, int] = {}
    tokens_by_day: dict[str, int] = {}
    for day, c, t in daily_rows:
        if day:
            events_by_day[day] = int(c)
            tokens_by_day[day] = int(t)
    daily = [
        DailyPoint(**p)
        for p in tc.assemble_daily(
            tc.daily_keys_desc(events_by_day, tokens_by_day),
            events_by_day,
            tokens_by_day,
        )
    ]

    async def _group(col) -> list[NameCount]:
        rows = (
            await db.execute(
                select(
                    col,
                    func.count(KbTokenCutterEvent.id),
                    func.coalesce(func.sum(KbTokenCutterEvent.est_tokens), 0),
                ).where(w).group_by(col).order_by(func.count(KbTokenCutterEvent.id).desc())
            )
        ).all()
        return [
            NameCount(name=(k if k is not None else "(unknown)"), count=int(c), est_tokens=int(t))
            for k, c, t in rows
        ]

    by_reason = await _group(KbTokenCutterEvent.reason)
    by_pc = await _group(KbTokenCutterEvent.pc)
    by_tool = await _group(KbTokenCutterEvent.tool)

    recent_rows = await db.execute(
        select(KbTokenCutterEvent).where(w).order_by(KbTokenCutterEvent.occurred_at.desc()).limit(15)
    )
    recent = [
        RecentEvent(
            occurred_at=e.occurred_at,
            pc=e.pc,
            tool=e.tool,
            reason=e.reason,
            target_kb=e.target_kb,
            est_tokens=e.est_tokens,
        )
        for e in recent_rows.scalars().all()
    ]

    return TokenCutterStats(
        days=days,
        since=since.strftime("%Y-%m-%d"),
        totals=TokenCutterTotals(
            events=total_events,
            est_tokens=int(total_tokens),
            est_tokens_human=tc.humanize_tokens(int(total_tokens)),
            pcs=total_pcs,
            by_reason=by_reason,
            by_pc=by_pc,
            by_tool=by_tool,
        ),
        daily=daily,
        recent=recent,
    )
