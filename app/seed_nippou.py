"""各部署の日報（DailyReport）の冪等backfillシード。

社長依頼（2026-06-19）: /nippou の日報を各部署30日分に充実させる。
データは app/data/nippou_backfill.json（リポジトリ同梱）。中身は #日報(Slack)の
実投稿（社員の実活動＝事実）を日別×部署に束ねたもの。起動時にバックグラウンドで
一度だけ冪等に取り込む。

設計方針（seed_ranraners と同方針）:
- 起動をブロックしない（lifespan から create_task で起動）。
- 冪等かつ非破壊: 既に存在する (department, report_date) は触らない（INSERTのみ）。
  → 過去セッションで作り込んだ精緻な日報（metrics付き等）を上書きしない。
- 部分耐性: 20件ごとに commit。
- 競合耐性: UNIQUE(department, report_date) 違反は savepoint で握って当該のみスキップ
  （多重起動レース対策）。
- 失敗しても本体に影響させない（全例外を握る・ログのみ）。
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models import DailyReport

_logger = logging.getLogger(__name__)
_DATA_PATH = Path(__file__).parent / "data" / "nippou_backfill.json"
_COMMIT_EVERY = 20


def _to_date(v) -> datetime.date | None:
    if isinstance(v, datetime.date):
        return v
    try:
        return datetime.date.fromisoformat(str(v))
    except Exception:  # noqa: BLE001
        return None


async def maybe_seed_nippou() -> None:
    """app/data/nippou_backfill.json を冪等・非破壊に取り込む。"""
    try:
        if not _DATA_PATH.exists():
            _logger.info("nippou seed: data file not found, skip")
            return
        reports = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        if not isinstance(reports, list) or not reports:
            return

        async with async_session() as db:
            # 既存の (department, report_date) 集合を取得し、不足分だけ入れる
            existing_rows = (
                await db.execute(select(DailyReport.department, DailyReport.report_date))
            ).all()
            existing = {(d, rd) for (d, rd) in existing_rows}

            inserted = 0
            skipped = 0
            for r in reports:
                dept = r.get("department")
                rd = _to_date(r.get("report_date"))
                if not dept or rd is None:
                    skipped += 1
                    continue
                if (dept, rd) in existing:
                    skipped += 1
                    continue

                row = DailyReport(
                    department=dept,
                    report_date=rd,
                    bucho=r.get("bucho"),
                    bucho_comment=r.get("bucho_comment"),
                    title=r.get("title"),
                    summary=r.get("summary"),
                    body_md=r.get("body_md"),
                    metrics=r.get("metrics"),
                )
                try:
                    async with db.begin_nested():  # savepoint: UNIQUE違反レースは当該のみ握る
                        db.add(row)
                        await db.flush()
                except IntegrityError:
                    skipped += 1
                    continue

                existing.add((dept, rd))
                inserted += 1
                if inserted % _COMMIT_EVERY == 0:
                    await db.commit()

            await db.commit()
            _logger.info(
                "nippou seed done: inserted=%s skipped=%s (total=%s)",
                inserted,
                skipped,
                len(reports),
            )
    except Exception as e:  # noqa: BLE001 — シード失敗は本体に波及させない
        _logger.warning("nippou seed error (ignored): %s", e)
