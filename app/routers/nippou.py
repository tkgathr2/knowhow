"""各部署の日次レポート（日報）API。

POST /api/nippou         … upsert（department+report_date で存在すれば UPDATE、無ければ INSERT）。
                           write系なので X-API-Key（KB_API_KEY）保護。
GET  /api/nippou         … 新しい順に一覧（?department=stepup&limit=30）。department 未指定なら全部署。
GET  /api/nippou/latest  … 各 department の最新1件ずつ（ダッシュボードTOP用）。

GET は開放（ナレッジ系と同様、ブラウザから鍵なしで叩ける）。日報の表示の本拠を
Notion→knowhow（Web）に移すための受け皿。まずステップアップ（'stepup'）から。
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_key
from app.database import get_db
from app.models import DailyReport

router = APIRouter(tags=["nippou"])

_WRITE_GUARD = [Depends(require_api_key)]

# 表示順や絞り込みで使う部署キー。'stepup' が本命、他は将来用。
DEPARTMENTS = ("stepup", "soumu", "koutsu")


class NippouRequest(BaseModel):
    department: str
    report_date: date
    bucho: str | None = None
    bucho_comment: str | None = None
    title: str | None = None
    summary: str | None = None
    body_md: str | None = None
    metrics: dict | None = None


class NippouItem(BaseModel):
    id: int
    department: str
    report_date: date
    bucho: str | None = None
    bucho_comment: str | None = None
    title: str | None = None
    summary: str | None = None
    body_md: str | None = None
    metrics: dict | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NippouUpsertResponse(BaseModel):
    id: int
    department: str
    report_date: date
    created: bool  # True=新規INSERT / False=既存UPDATE
    message: str


class NippouListResponse(BaseModel):
    count: int
    items: list[NippouItem]


def _to_item(row: DailyReport) -> NippouItem:
    return NippouItem(
        id=row.id,
        department=row.department,
        report_date=row.report_date,
        bucho=row.bucho,
        bucho_comment=row.bucho_comment,
        title=row.title,
        summary=row.summary,
        body_md=row.body_md,
        metrics=row.metrics,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("/nippou", response_model=NippouUpsertResponse, dependencies=_WRITE_GUARD)
async def upsert_nippou(req: NippouRequest, db: AsyncSession = Depends(get_db)) -> NippouUpsertResponse:
    existing = await db.execute(
        select(DailyReport).where(
            DailyReport.department == req.department,
            DailyReport.report_date == req.report_date,
        )
    )
    row = existing.scalar_one_or_none()
    created = row is None
    if row is None:
        row = DailyReport(department=req.department, report_date=req.report_date)
        db.add(row)

    row.bucho = req.bucho
    row.bucho_comment = req.bucho_comment
    row.title = req.title
    row.summary = req.summary
    row.body_md = req.body_md
    row.metrics = req.metrics

    await db.commit()
    await db.refresh(row)

    return NippouUpsertResponse(
        id=row.id,
        department=row.department,
        report_date=row.report_date,
        created=created,
        message="日報を登録しました" if created else "日報を更新しました",
    )


@router.get("/nippou", response_model=NippouListResponse)
async def list_nippou(
    department: str | None = None,
    limit: int = 30,
    db: AsyncSession = Depends(get_db),
) -> NippouListResponse:
    limit = max(1, min(limit, 365))
    stmt = select(DailyReport)
    if department:
        stmt = stmt.where(DailyReport.department == department)
    stmt = stmt.order_by(DailyReport.report_date.desc(), DailyReport.id.desc()).limit(limit)

    rows = (await db.execute(stmt)).scalars().all()
    items = [_to_item(r) for r in rows]
    return NippouListResponse(count=len(items), items=items)


@router.get("/nippou/latest", response_model=NippouListResponse)
async def latest_nippou(db: AsyncSession = Depends(get_db)) -> NippouListResponse:
    """各 department の最新1件ずつ（ダッシュボードTOP用）。"""
    stmt = select(DailyReport).order_by(DailyReport.report_date.desc(), DailyReport.id.desc())
    rows = (await db.execute(stmt)).scalars().all()

    seen: dict[str, DailyReport] = {}
    for r in rows:
        if r.department not in seen:
            seen[r.department] = r

    items = [_to_item(seen[d]) for d in DEPARTMENTS if d in seen]
    # 既知部署以外（将来の追加）も拾えるよう、未知部署はその後ろに付ける
    items += [_to_item(r) for dep, r in seen.items() if dep not in DEPARTMENTS]
    return NippouListResponse(count=len(items), items=items)
