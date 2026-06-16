"""POST /koe/signals の保存ループ（SAVEPOINT 単位の冪等 dedup）の動作実証。

実DB（aiosqlite）に対し、ユニーク制約違反の行が混ざっても
「先に保存済みの行は残り、重複行だけがスキップされる」ことを確かめる。
これは begin_nested を使わず db.rollback() するとセッション全体が巻き戻る
バグ（先行保存が消える）への回帰防止。
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class _Base(DeclarativeBase):
    pass


class _Sig(_Base):
    __tablename__ = "sig"
    id = Column(Integer, primary_key=True, autoincrement=True)
    dedup_hash = Column(String, nullable=False)
    title = Column(String, nullable=False)
    __table_args__ = (UniqueConstraint("dedup_hash", name="uq_sig_dedup"),)


async def _save_loop(db: AsyncSession, items: list[dict]) -> int:
    """routers/koe.py の保存ループと同じ構造（SAVEPOINT 単位）。"""
    saved = 0
    for it in items:
        row = _Sig(dedup_hash=it["dedup_hash"], title=it["title"])
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            continue
        saved += 1
    await db.commit()
    return saved


@pytest.mark.asyncio
async def test_savepoint_loop_keeps_prior_rows_on_duplicate():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)

    items = [
        {"dedup_hash": "h1", "title": "A"},
        {"dedup_hash": "h2", "title": "B"},
        {"dedup_hash": "h1", "title": "A-dup"},  # h1 重複 → スキップ
        {"dedup_hash": "h3", "title": "C"},
    ]
    async with AsyncSession(engine) as db:
        saved = await _save_loop(db, items)

    assert saved == 3  # h1,h2,h3（重複 h1 の2件目だけ落ちる）

    # 先に保存した A,B が重複行のロールバックで消えていないこと
    async with AsyncSession(engine) as db:
        from sqlalchemy import select

        rows = (await db.execute(select(_Sig.title).order_by(_Sig.id))).scalars().all()
    assert rows == ["A", "B", "C"]
    await engine.dispose()
