import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.auth import require_api_key
from app.middleware import BrowserAuthMiddleware
from app.routers import (
    admin,
    anthropic_cost,
    auth_oauth,
    auto_learn,
    bulk,
    dashboard,
    devin,
    digest,
    external,
    feedback,
    health,
    ingest,
    intelligence,
    koe,
    metabolize,
    nightly,
    nippou,
    search,
    token_cutter,
    webhook,
)

_STATIC_DIR = Path(__file__).parent / "static"
_logger = logging.getLogger(__name__)
_background_tasks: set = set()  # 背景タスクの強参照保持（GCで消えるのを防ぐ）


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
    # らんさ〜ず知見の冪等シード（社長依頼 2026-06-14）。起動をブロックしないよう背景タスクで実行。
    try:
        from app.seed_ranraners import maybe_seed_ranraners

        task = asyncio.create_task(maybe_seed_ranraners())
        _background_tasks.add(task)
        task.add_done_callback(
            lambda t: (
                _background_tasks.discard(t),
                t.cancelled() or (t.exception() and _logger.warning("ranraners seed task failed: %s", t.exception())),
            )
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning("ranraners seed scheduling failed (ignored): %s", e)
    # 各部署の日報30日分の冪等backfill（社長依頼 2026-06-19）。既存日報は上書きしない。
    try:
        from app.seed_nippou import maybe_seed_nippou

        task2 = asyncio.create_task(maybe_seed_nippou())
        _background_tasks.add(task2)
        task2.add_done_callback(
            lambda t: (
                _background_tasks.discard(t),
                t.cancelled() or (t.exception() and _logger.warning("nippou seed task failed: %s", t.exception())),
            )
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning("nippou seed scheduling failed (ignored): %s", e)
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

# ブラウザ向けGoogleログイン保護（資格情報未設定の間は素通り＝挙動不変）
app.add_middleware(BrowserAuthMiddleware)

# Googleログイン動線（/auth/login, /auth/callback, /auth/logout, /auth/me）
app.include_router(auth_oauth.router)

# 認証なしで開放: ヘルスチェックのみ
app.include_router(health.router)

# HO-83: read系=開放 / write系=保護（KB_API_KEY 設定時のみ X-API-Key 必須）。
# KB_API_KEY 未設定の間は require_api_key が素通り＝全EP開放のまま＝挙動不変。
# 設定後は write系のみ保護。read系（ダッシュボード/検索/recall）はブラウザから鍵なしで
# 叩かれる＆ナレッジは機密性が低いため開放を維持（ブラウザJSに鍵を埋めない方針）。
_protected = [Depends(require_api_key)]

# --- 読み取り系：開放 ---
app.include_router(dashboard.router, prefix="/api")  # /stats /recent /tags /chunks/{id} /search/cross-project
app.include_router(search.router, prefix="/api")     # /search /search/hybrid
app.include_router(devin.router, prefix="/api")      # /devin/recall=開放, /devin/memorize=EP単位で保護
# token-cutter: /event=開放(各PCのフックが鍵なしでPOST) / /stats=閲覧保護(middleware)
app.include_router(token_cutter.router, prefix="/api")
# anthropic-cost: /receipts=EP単位でKB_API_KEY保護 / /stats=閲覧保護(middleware)
app.include_router(anthropic_cost.router, prefix="/api")
# 学びの自動ingest受け口（/auto-learn＝EP単位でKB_API_KEY保護。SessionEndフックが叩く）
app.include_router(auto_learn.router, prefix="/api")
# 1日のダイジェスト: /digest/daily=閲覧開放（middlewareでブラウザ保護） / /digest/run=EP単位で保護
app.include_router(digest.router, prefix="/api")
# 各部署の日報: GET=開放（ダッシュボード/ビューアが鍵なしで叩く） / POST=EP単位でKB_API_KEY保護
app.include_router(nippou.router, prefix="/api")

# --- 書き込み・バッチ・外部取込系：保護 ---
app.include_router(ingest.router, prefix="/api", dependencies=_protected)
app.include_router(feedback.router, prefix="/api", dependencies=_protected)
app.include_router(bulk.router, prefix="/api", dependencies=_protected)
app.include_router(intelligence.router, prefix="/api", dependencies=_protected)
app.include_router(external.router, prefix="/api", dependencies=_protected)
app.include_router(nightly.router, prefix="/api", dependencies=_protected)
# 学びの新陳代謝（候補取得＋一括deprecated化）。X-API-Key（KB_API_KEY）で保護。
app.include_router(metabolize.router, prefix="/api", dependencies=_protected)
# ロア（録音資産）：write系(ingest/process/digest生成)はEP単位でX-API-Key保護（koe.py の _WRITE_GUARD）。
# read系(GET digest/recordings)はブラウザ=Googleログイン / バッチ=X-API-Key（middleware の保護プレフィックス）。
app.include_router(koe.router, prefix="/api")

# Webhook は API キーではなく GitHub HMAC 署名（X-Hub-Signature-256）で検証するため対象外
app.include_router(webhook.router, prefix="/api")

# HO-83 移行用 管理import。X-Admin-Key（ADMIN_IMPORT_KEY）で別系統認証するため _protected は付けない。
# ADMIN_IMPORT_KEY 未設定なら /api/admin/* は 503（誤って全開放しない安全側）。
app.include_router(admin.router, prefix="/api")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # ブラウザが自動取得するファビコン。未定義だと {"detail":"Not Found"} が
    # 出てシステムが壊れているように見えるため、必ずアイコンを返す。
    return FileResponse(_STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg():
    return FileResponse(_STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_touch_icon():
    return FileResponse(_STATIC_DIR / "apple-touch-icon.png", media_type="image/png")


@app.get("/growth", include_in_schema=False)
async def growth_page():
    return FileResponse(_STATIC_DIR / "growth.html")


@app.get("/lore", include_in_schema=False)
async def lore_page():
    return FileResponse(_STATIC_DIR / "lore.html")


@app.get("/daily", include_in_schema=False)
async def daily_page():
    return FileResponse(_STATIC_DIR / "daily.html")


@app.get("/bucho", include_in_schema=False)
async def bucho_page():
    return FileResponse(_STATIC_DIR / "bucho.html")


@app.get("/nippou", include_in_schema=False)
async def nippou_page():
    return FileResponse(_STATIC_DIR / "nippou.html")


@app.get("/bucho/{key}", include_in_schema=False)
async def bucho_detail_page(key: str):
    return FileResponse(_STATIC_DIR / "bucho-detail.html")


@app.get("/token-cutter", include_in_schema=False)
async def token_cutter_page():
    return FileResponse(_STATIC_DIR / "token-cutter.html")


@app.get("/anthropic-cost", include_in_schema=False)
async def anthropic_cost_page():
    return FileResponse(_STATIC_DIR / "anthropic-cost.html")


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
