import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.auth import require_api_key
from app.routers import admin, bulk, dashboard, devin, external, feedback, health, ingest, intelligence, nightly, search, webhook

_STATIC_DIR = Path(__file__).parent / "static"
_logger = logging.getLogger(__name__)


def _run_migrations() -> None:
    import os

    import psycopg2

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        _logger.warning("DATABASE_URL not set, skipping migrations")
        return

    migration_dir = Path(__file__).parent.parent / "db"
    migration_files = sorted(migration_dir.glob("v*.sql"))
    if not migration_files:
        return

    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            for mf in migration_files:
                sql = mf.read_text(encoding="utf-8")
                try:
                    cur.execute(sql)
                    _logger.info("Migration applied: %s", mf.name)
                except Exception as e:
                    _logger.warning("Migration %s (may already be applied): %s", mf.name, e)
        conn.close()
    except Exception as e:
        _logger.warning("Migration runner error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _run_migrations()
    yield


app = FastAPI(
    title="ノウハウキング君 API",
    description="AI外部記憶基盤 - 開発ログを構造化・ベクトル化し再利用可能にする",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 認証なしで開放: ヘルスチェックのみ
app.include_router(health.router)

# API キー保護対象（KB_API_KEY 設定時のみ有効。未設定なら従来通り開放）
_protected = [Depends(require_api_key)]
app.include_router(ingest.router, prefix="/api", dependencies=_protected)
app.include_router(search.router, prefix="/api", dependencies=_protected)
app.include_router(feedback.router, prefix="/api", dependencies=_protected)
app.include_router(devin.router, prefix="/api", dependencies=_protected)
app.include_router(dashboard.router, prefix="/api", dependencies=_protected)
app.include_router(bulk.router, prefix="/api", dependencies=_protected)
app.include_router(intelligence.router, prefix="/api", dependencies=_protected)
app.include_router(external.router, prefix="/api", dependencies=_protected)
app.include_router(nightly.router, prefix="/api", dependencies=_protected)

# Webhook は API キーではなく GitHub HMAC 署名（X-Hub-Signature-256）で検証するため対象外
app.include_router(webhook.router, prefix="/api")

# HO-83 移行用 管理import。X-Admin-Key（ADMIN_IMPORT_KEY）で別系統認証するため _protected は付けない。
# ADMIN_IMPORT_KEY 未設定なら /api/admin/* は 503（誤って全開放しない安全側）。
app.include_router(admin.router, prefix="/api")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(_STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
