"""コストカッターα（cost-cutter）: 削減率ダッシュボード API。

GET /cost-cutter/stats : 実費用(anthropic-cost の領収書)の月次から「ピーク月→当月着地予測」
  の削減額/削減率を出し、token-cutter の推定節約額を併記する（閲覧保護下＝middleware）。
既存テーブル（領収書・トークンカッターイベント）を直接読む＝内部HTTP非依存・pytestで検証可能。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import anthropic_cost as ac
from app import cost_cutter as cc
from app import token_cutter as tc
from app.database import get_db
from app.models import KbAnthropicReceipt, KbTokenCutterEvent

router = APIRouter(tags=["cost-cutter"])

_JST = timezone(timedelta(hours=9))
_MONTH = func.to_char(KbAnthropicReceipt.receipt_date, "YYYY-MM")


class MonthlyPoint(BaseModel):
    month: str
    total_jpy: int


class TokenCutterContribution(BaseModel):
    days: int
    events: int
    est_tokens: int
    est_tokens_human: str
    saved_jpy: int
    saved_jpy_human: str


class CostCutterStats(BaseModel):
    today: str
    baseline_month: str | None
    baseline_jpy: int
    baseline_jpy_human: str
    current_month: str
    current_jpy: int
    current_jpy_human: str
    projection_jpy: int
    projection_jpy_human: str
    reduction_jpy: int
    reduction_jpy_human: str
    reduction_pct: float
    annual_saving_jpy: int
    annual_saving_jpy_human: str
    days_elapsed: int
    days_in_month: int
    monthly: list[MonthlyPoint]
    token_cutter: TokenCutterContribution


@router.get("/cost-cutter/stats", response_model=CostCutterStats)
async def get_stats(
    months: int = 6, tc_days: int = 30, db: AsyncSession = Depends(get_db)
) -> CostCutterStats:
    months = max(2, min(months, 24))
    tc_days = max(1, min(tc_days, 120))
    today = datetime.now(_JST).date()
    month_keys = ac.recent_month_keys(today, months)
    since = date.fromisoformat(month_keys[0] + "-01")
    w = KbAnthropicReceipt.receipt_date >= since

    rows = (
        await db.execute(
            select(
                _MONTH,
                func.coalesce(func.sum(KbAnthropicReceipt.total_usd), 0.0),
                func.coalesce(func.sum(KbAnthropicReceipt.total_jpy), 0),
                func.count(KbAnthropicReceipt.id),
            )
            .where(w)
            .group_by(_MONTH)
        )
    ).all()
    monthly_full = ac.assemble_monthly(
        [
            {"month": m, "kind": "other", "total_usd": u, "total_jpy": j, "count": c}
            for m, u, j, c in rows
        ],
        month_keys,
    )
    monthly = [
        {"month": m["month"], "total_jpy": int(m["total_jpy"])} for m in monthly_full
    ]

    current_month = month_keys[-1]
    cur = next(
        (m for m in monthly if m["month"] == current_month),
        {"month": current_month, "total_jpy": 0},
    )
    current_jpy = int(cur["total_jpy"])
    projection_jpy = int(ac.project_month_end(float(current_jpy), today))

    baseline = cc.pick_baseline(monthly, current_month)
    baseline_month = baseline["month"] if baseline else None
    baseline_jpy = int(baseline["total_jpy"]) if baseline else 0

    red = cc.reduction(baseline_jpy, projection_jpy)
    annual = cc.annualized_saving(baseline_jpy, projection_jpy)

    now = datetime.now(timezone.utc)
    tcw = KbTokenCutterEvent.occurred_at >= (now - timedelta(days=tc_days))
    tc_row = (
        await db.execute(
            select(
                func.count(KbTokenCutterEvent.id),
                func.coalesce(func.sum(KbTokenCutterEvent.est_tokens), 0),
            ).where(tcw)
        )
    ).one()
    tc_events, tc_tokens = int(tc_row[0]), int(tc_row[1])
    tc_money = tc.estimate_money(tc_tokens)

    return CostCutterStats(
        today=today.isoformat(),
        baseline_month=baseline_month,
        baseline_jpy=baseline_jpy,
        baseline_jpy_human=ac.humanize_jpy(baseline_jpy),
        current_month=current_month,
        current_jpy=current_jpy,
        current_jpy_human=ac.humanize_jpy(current_jpy),
        projection_jpy=projection_jpy,
        projection_jpy_human=ac.humanize_jpy(projection_jpy),
        reduction_jpy=red["reduction_jpy"],
        reduction_jpy_human=ac.humanize_jpy(red["reduction_jpy"]),
        reduction_pct=red["reduction_pct"],
        annual_saving_jpy=annual,
        annual_saving_jpy_human=ac.humanize_jpy(annual),
        days_elapsed=today.day,
        days_in_month=ac.days_in_month(today),
        monthly=[MonthlyPoint(**m) for m in monthly],
        token_cutter=TokenCutterContribution(
            days=tc_days,
            events=tc_events,
            est_tokens=tc_tokens,
            est_tokens_human=tc.humanize_tokens(tc_tokens),
            saved_jpy=tc_money["jpy"],
            saved_jpy_human=tc.humanize_jpy(tc_money["jpy"]),
        ),
    )
