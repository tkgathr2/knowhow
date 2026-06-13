"""Anthropic費用ダッシュボード API。

POST /anthropic-cost/receipts : 領収書の取込（receipt_noで冪等upsert・X-API-Key保護）。
GET  /anthropic-cost/stats    : 月次・日別・種別の集計（閲覧保護下＝middleware）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import anthropic_cost as ac
from app.auth import require_api_key
from app.database import get_db
from app.models import KbAnthropicReceipt

router = APIRouter(tags=["anthropic-cost"])

_JST = timezone(timedelta(hours=9))


async def _fetch_usdjpy() -> float | None:
    """USD/JPYの実勢レートを取得（失敗時はNone＝JPY換算なしで保存）。"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://open.er-api.com/v6/latest/USD")
            r.raise_for_status()
            rate = float(r.json()["rates"]["JPY"])
            return rate if rate > 0 else None
    except Exception:
        return None


class ReceiptIn(BaseModel):
    receipt_no: str = Field(min_length=1, max_length=40)
    receipt_date: date
    description: str = Field(min_length=1, max_length=200)
    subtotal_usd: float = Field(ge=0)
    tax_usd: float = Field(default=0, ge=0)
    total_usd: float = Field(ge=0)
    usdjpy: float | None = Field(default=None, gt=0)
    kind: str | None = None


class ReceiptsIn(BaseModel):
    receipts: list[ReceiptIn] = Field(min_length=1, max_length=200)


class ReceiptsOut(BaseModel):
    ok: bool
    upserted: int
    usdjpy: float | None


@router.post(
    "/anthropic-cost/receipts",
    response_model=ReceiptsOut,
    dependencies=[Depends(require_api_key)],
)
async def upsert_receipts(
    body: ReceiptsIn, db: AsyncSession = Depends(get_db)
) -> ReceiptsOut:
    rate = next((r.usdjpy for r in body.receipts if r.usdjpy), None)
    if rate is None:
        rate = await _fetch_usdjpy()

    upserted = 0
    for r in body.receipts:
        row_rate = r.usdjpy or rate
        values = dict(
            receipt_no=r.receipt_no.strip(),
            receipt_date=r.receipt_date,
            description=r.description.strip(),
            kind=(r.kind if r.kind in ac.KINDS else ac.classify_kind(r.description)),
            subtotal_usd=r.subtotal_usd,
            tax_usd=r.tax_usd,
            total_usd=r.total_usd,
            usdjpy=row_rate,
            total_jpy=ac.to_jpy(r.total_usd, row_rate),
        )
        stmt = pg_insert(KbAnthropicReceipt).values(**values)
        # 再取込時は金額系のみ更新（既存のレート/JPYは初回取込時点を保持）
        stmt = stmt.on_conflict_do_update(
            index_elements=[KbAnthropicReceipt.receipt_no],
            set_={
                "receipt_date": stmt.excluded.receipt_date,
                "description": stmt.excluded.description,
                "kind": stmt.excluded.kind,
                "subtotal_usd": stmt.excluded.subtotal_usd,
                "tax_usd": stmt.excluded.tax_usd,
                "total_usd": stmt.excluded.total_usd,
            },
        )
        await db.execute(stmt)
        upserted += 1
    await db.commit()
    return ReceiptsOut(ok=True, upserted=upserted, usdjpy=rate)


class MonthPoint(BaseModel):
    month: str
    total_usd: float
    total_jpy: int
    receipts: int
    by_kind: dict[str, float]


class DailyPoint(BaseModel):
    date: str
    total_usd: float
    total_jpy: int


class RecentReceipt(BaseModel):
    receipt_date: date
    receipt_no: str
    description: str
    kind: str
    total_usd: float
    total_jpy: int | None


class CurrentMonth(BaseModel):
    month: str
    total_usd: float
    total_jpy: int
    total_jpy_human: str
    projection_usd: float
    projection_jpy: int
    projection_jpy_human: str
    days_elapsed: int
    days_in_month: int


class CostStats(BaseModel):
    today: str
    usdjpy_latest: float | None
    current: CurrentMonth
    monthly: list[MonthPoint]
    daily: list[DailyPoint]
    recent: list[RecentReceipt]


_MONTH = func.to_char(KbAnthropicReceipt.receipt_date, "YYYY-MM")
_DAY = func.to_char(KbAnthropicReceipt.receipt_date, "YYYY-MM-DD")


@router.get("/anthropic-cost/stats", response_model=CostStats)
async def get_stats(
    months: int = 6, db: AsyncSession = Depends(get_db)
) -> CostStats:
    months = max(1, min(months, 24))
    today = datetime.now(_JST).date()
    month_keys = ac.recent_month_keys(today, months)
    since = date.fromisoformat(month_keys[0] + "-01")
    w = KbAnthropicReceipt.receipt_date >= since

    monthly_rows = (
        await db.execute(
            select(
                _MONTH,
                KbAnthropicReceipt.kind,
                func.coalesce(func.sum(KbAnthropicReceipt.total_usd), 0.0),
                func.coalesce(func.sum(KbAnthropicReceipt.total_jpy), 0),
                func.count(KbAnthropicReceipt.id),
            ).where(w).group_by(_MONTH, KbAnthropicReceipt.kind)
        )
    ).all()
    monthly = ac.assemble_monthly(
        [
            {"month": m, "kind": k, "total_usd": u, "total_jpy": j, "count": c}
            for m, k, u, j, c in monthly_rows
        ],
        month_keys,
    )

    cur = monthly[-1]
    proj_usd = ac.project_month_end(cur["total_usd"], today)
    proj_jpy = int(ac.project_month_end(float(cur["total_jpy"]), today))
    current = CurrentMonth(
        month=cur["month"],
        total_usd=cur["total_usd"],
        total_jpy=cur["total_jpy"],
        total_jpy_human=ac.humanize_jpy(cur["total_jpy"]),
        projection_usd=proj_usd,
        projection_jpy=proj_jpy,
        projection_jpy_human=ac.humanize_jpy(proj_jpy),
        days_elapsed=today.day,
        days_in_month=ac.days_in_month(today),
    )

    cur_month_start = date(today.year, today.month, 1)
    daily_rows = (
        await db.execute(
            select(
                _DAY,
                func.coalesce(func.sum(KbAnthropicReceipt.total_usd), 0.0),
                func.coalesce(func.sum(KbAnthropicReceipt.total_jpy), 0),
            )
            .where(KbAnthropicReceipt.receipt_date >= cur_month_start)
            .group_by(_DAY)
            .order_by(_DAY)
        )
    ).all()
    daily = [
        DailyPoint(date=d, total_usd=round(float(u), 2), total_jpy=int(j))
        for d, u, j in daily_rows
    ]

    recent_rows = await db.execute(
        select(KbAnthropicReceipt)
        .order_by(KbAnthropicReceipt.receipt_date.desc(), KbAnthropicReceipt.id.desc())
        .limit(15)
    )
    recent = [
        RecentReceipt(
            receipt_date=r.receipt_date,
            receipt_no=r.receipt_no,
            description=r.description,
            kind=r.kind,
            total_usd=r.total_usd,
            total_jpy=r.total_jpy,
        )
        for r in recent_rows.scalars().all()
    ]

    latest_rate_row = (
        await db.execute(
            select(KbAnthropicReceipt.usdjpy)
            .where(KbAnthropicReceipt.usdjpy.isnot(None))
            .order_by(KbAnthropicReceipt.receipt_date.desc(), KbAnthropicReceipt.id.desc())
            .limit(1)
        )
    ).scalar()

    return CostStats(
        today=today.isoformat(),
        usdjpy_latest=latest_rate_row,
        current=current,
        monthly=[MonthPoint(**m) for m in monthly],
        daily=daily,
        recent=recent,
    )
