"""日報（daily_reports）API の upsert 往復・重複UPDATE・latest を実DB（aiosqlite）で実証。

routers/nippou.py の POST/GET を、in-memory sqlite に対して TestClient で叩く。
- POST→GET の往復で同じ日報が読み戻せること
- 同一(department, report_date)の再POSTで行が増えず UPDATE されること
- GET /nippou/latest が部署ごとの最新1件を返すこと
本番は asyncpg/Postgres。aiosqlite は test 専用（test_koe_signal_savepoint と同様）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import require_api_key
from app.database import get_db
from app.main import app
from app.models import Base, DailyReport


@pytest.fixture()
def client():
    # StaticPool＝全コネクションで1つの in-memory DB を共有（テーブル作成とセッションが同じDBを見る）
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    _created = {"done": False}

    async def _override_get_db():
        # スキーマ作成はリクエストと同じイベントループ内で1回だけ（StaticPool の単一接続を共有）
        if not _created["done"]:
            async with engine.begin() as conn:
                # daily_reports だけ作る（pgvector など他モデルは sqlite で作れないため）
                await conn.run_sync(Base.metadata.create_all, tables=[DailyReport.__table__])
            _created["done"] = True
        async with session_factory() as session:
            yield session

    # KB_API_KEY が環境にあってもなくても決定的にするため、認証を無効化（write系の動作確認が目的）
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_api_key] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(require_api_key, None)


def test_post_then_get_roundtrip(client):
    payload = {
        "department": "stepup",
        "report_date": "2026-06-17",
        "bucho": "SU参謀 本部長 橘 遼一",
        "bucho_comment": "面談が伸びている。今週の山場は紹介数。",
        "title": "6/17 ステップアップ日報",
        "summary": "売上好調\n面談+3\n内定1",
        "body_md": "# 概況\n- 面談3件\n- 内定1件",
        "metrics": {"売上": 160000, "目標": 200000, "達成率": "80%"},
    }
    r = client.post("/api/nippou", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["department"] == "stepup"
    rid = body["id"]

    g = client.get("/api/nippou?department=stepup")
    assert g.status_code == 200
    data = g.json()
    assert data["count"] == 1
    item = data["items"][0]
    assert item["id"] == rid
    assert item["title"] == "6/17 ステップアップ日報"
    assert item["bucho"] == "SU参謀 本部長 橘 遼一"
    assert item["metrics"]["達成率"] == "80%"
    assert item["body_md"].startswith("# 概況")


def test_repost_same_date_updates_not_duplicates(client):
    base = {"department": "stepup", "report_date": "2026-06-17", "title": "v1", "summary": "初版"}
    r1 = client.post("/api/nippou", json=base)
    assert r1.json()["created"] is True
    rid = r1.json()["id"]

    upd = {**base, "title": "v2", "summary": "改訂版", "bucho_comment": "追記しました"}
    r2 = client.post("/api/nippou", json=upd)
    assert r2.status_code == 200
    assert r2.json()["created"] is False  # UPDATE
    assert r2.json()["id"] == rid          # 同じ行

    g = client.get("/api/nippou?department=stepup")
    data = g.json()
    assert data["count"] == 1               # 重複していない
    item = data["items"][0]
    assert item["title"] == "v2"
    assert item["summary"] == "改訂版"
    assert item["bucho_comment"] == "追記しました"


def test_latest_returns_one_per_department(client):
    # stepup: 2日分 → 新しい方が latest
    client.post("/api/nippou", json={"department": "stepup", "report_date": "2026-06-16", "title": "su-old"})
    client.post("/api/nippou", json={"department": "stepup", "report_date": "2026-06-17", "title": "su-new"})
    # soumu: 1日分
    client.post("/api/nippou", json={"department": "soumu", "report_date": "2026-06-15", "title": "soumu-1"})

    r = client.get("/api/nippou/latest")
    assert r.status_code == 200
    items = r.json()["items"]
    by_dept = {i["department"]: i for i in items}
    assert set(by_dept.keys()) == {"stepup", "soumu"}
    assert by_dept["stepup"]["title"] == "su-new"
    assert by_dept["stepup"]["report_date"] == "2026-06-17"
    assert by_dept["soumu"]["title"] == "soumu-1"
    # DEPARTMENTS の順序（stepup が soumu より先）
    assert items[0]["department"] == "stepup"


def test_list_all_departments_when_unspecified(client):
    client.post("/api/nippou", json={"department": "stepup", "report_date": "2026-06-17", "title": "a"})
    client.post("/api/nippou", json={"department": "soumu", "report_date": "2026-06-17", "title": "b"})
    g = client.get("/api/nippou")
    assert g.json()["count"] == 2
