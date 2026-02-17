from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import bulk, dashboard, devin, feedback, health, ingest, search

app = FastAPI(
    title="つみあげくん API",
    description="AI外部記憶基盤 - 開発ログを構造化・ベクトル化し再利用可能にする",
    version="0.1.0",
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
