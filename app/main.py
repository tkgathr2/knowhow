import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routers import bulk, dashboard, devin, external, feedback, health, ingest, intelligence, search, webhook

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

app.include_router(health.router)
app.include_router(ingest.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(feedback.router, prefix="/api")
app.include_router(devin.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(bulk.router, prefix="/api")
app.include_router(intelligence.router, prefix="/api")
app.include_router(external.router, prefix="/api")
app.include_router(webhook.router, prefix="/api")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(_STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
