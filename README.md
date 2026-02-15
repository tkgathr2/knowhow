# つみあげくん (Knowhow Knowledge DB)

AI開発ログを構造化・ベクトル化し、
再利用可能な外部記憶基盤を構築するプロジェクト。
Devinと自動連動し、AIに長期記憶を与える外部脳。

## Phase
- Phase A: DB基盤 (PostgreSQL + pgvector + schema)
- Phase B: Search API + Ingest API (FastAPI)
- Phase C: Feedback Loop
- Phase D: Devin自動連動 (Knowledge / Playbook / Scheduled Sessions)

## Tech Stack
- Python 3.11+
- FastAPI + Uvicorn
- PostgreSQL 17 + pgvector (Railway)
- SQLAlchemy 2.0 (async)
- OpenAI Embeddings (text-embedding-3-large)

## Setup

```bash
poetry install
cp .env.example .env
# Edit .env with your DATABASE_URL and OPENAI_API_KEY
```

## Run

```bash
uvicorn app.main:app --reload
```

## Apply Schema

```bash
DATABASE_URL=postgresql://... python scripts/apply_schema.py
```

## API Endpoints

- `GET /health` - ヘルスチェック
- `POST /api/ingest` - セッションログ投入
- `POST /api/search` - ナレッジ検索
