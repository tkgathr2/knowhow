"""seed_nippou（日報の冪等backfill）と同梱データ app/data/nippou_backfill.json の健全性を実証。

- maybe_seed_nippou が「不足分のみINSERT・既存は非破壊・再実行で冪等」であること（in-memory sqlite）。
- 同梱JSONが各部署30日・必須項目あり・フッター混入なし、であること。
"""

from __future__ import annotations

import datetime
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.seed_nippou as seed_nippou
from app.models import Base, DailyReport

_DATA = Path(__file__).parent.parent / "app" / "data" / "nippou_backfill.json"


# ---------- 同梱データの健全性 ----------

def test_bundled_data_shape():
    reports = json.loads(_DATA.read_text(encoding="utf-8"))
    assert isinstance(reports, list)
    c = Counter(r["department"] for r in reports)
    assert c["stepup"] == 30
    assert c["soumu"] == 30
    assert c["koutsu"] == 30
    assert len(reports) == 90
    for r in reports:
        assert r["department"] in ("stepup", "soumu", "koutsu")
        assert r["report_date"] and len(r["report_date"]) == 10
        assert r["title"]
        assert r["body_md"] and r["body_md"].strip()
        # 実投稿の生フッター・TS行が混入していないこと
        assert "Powered by" not in r["body_md"]
        assert "Message TS" not in r["body_md"]
    # (department, report_date) は一意
    keys = [(r["department"], r["report_date"]) for r in reports]
    assert len(keys) == len(set(keys))


# ---------- シーダーの冪等・非破壊 ----------

async def test_seed_inserts_and_is_idempotent_and_nondestructive(monkeypatch, tmp_path):
    # asyncio_mode=auto。pytest-asyncio が管理するループ内で完結させる。
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[DailyReport.__table__])

    # テスト用データ：2部署×2日
    data = [
        {"department": "soumu", "report_date": "2026-06-10", "title": "soumu-10", "body_md": "x"},
        {"department": "soumu", "report_date": "2026-06-11", "title": "soumu-11", "body_md": "y"},
        {"department": "stepup", "report_date": "2026-06-10", "title": "step-10", "body_md": "z"},
    ]
    dp = tmp_path / "nip.json"
    dp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(seed_nippou, "async_session", factory)
    monkeypatch.setattr(seed_nippou, "_DATA_PATH", Path(dp))

    # 既存の精緻日報を1件先に入れておく（上書きされないことを確認）
    async with factory() as db:
        db.add(DailyReport(
            department="soumu", report_date=datetime.date(2026, 6, 10),
            title="EXISTING-KEEP", body_md="curated", summary="keep me",
        ))
        await db.commit()

    await seed_nippou.maybe_seed_nippou()  # 1回目
    await seed_nippou.maybe_seed_nippou()  # 2回目（冪等：増えない）

    async with factory() as db:
        rows = (await db.execute(select(DailyReport))).scalars().all()

    # 既存1 + 新規2（soumu-11, step-10）= 3。soumu-10 はキー衝突でスキップ。
    assert len(rows) == 3
    soumu_10 = [r for r in rows if r.department == "soumu" and str(r.report_date) == "2026-06-10"]
    assert len(soumu_10) == 1
    assert soumu_10[0].title == "EXISTING-KEEP"  # 非破壊：既存を上書きしない
    titles = sorted(r.title for r in rows)
    assert titles == ["EXISTING-KEEP", "soumu-11", "step-10"]
