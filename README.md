# ノウハウキング君 (Knowhow Knowledge DB)

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

## 自己成長エンジン Phase A（夜間ハートビート）

育つAI v5 の最小核。毎晩1パスで decay→再発検知→朝サマリを回す（追加のみ・後方互換）。

- `POST /api/nightly/run` … 冪等（run_date主キー）＋advisory lock＋catch-up（未完了の過去N日を遡って処理）。
  - decay（古く未想起chunkの信頼度減衰）／**北極星=再発した既知ミス件数**／採点燃料(recallログ件数) を集計し `kb_nightly_run.digest` に保存。
  - 動作確認: `{"dry_run": true}` で書き込みなし集計のみ。
- `GET /api/nightly/latest` … 直近の朝サマリ（digest）を取得。
- マイグレーション: `db/v7_self_growth.sql`（起動時に自動適用）。

### Railway cron 設定（ダッシュボード操作・別途）
毎晩02:00 JST（=17:00 UTC）に叩く想定。Railwayの Cron は「前回実行中なら次回をスキップ」する仕様のため、
catch-up（過去N日の未完了を遡る）を内蔵してある。さらに **healthchecks.io 等のDead-man's switch**で
「走らなかった夜」を検知する運用を推奨。
```
# 例: Railway cron schedule
0 17 * * *   ->   curl -fsS -X POST https://knowhow.up.railway.app/api/nightly/run -H 'Content-Type: application/json' -d '{}'
```
> 注: write系（/api/nightly/run 等）は KB_API_KEY 設定後に `-H "X-API-Key: $KB_API_KEY"` が必須。read系（/api/stats, /recent, /tags, /chunks, /search, /search/hybrid, /api/devin/recall 等）は KB_API_KEY 未設定でも引き続き開放。GitHub Actions ワークフローは `AUTH=(); if [ -n "${KB_API_KEY}" ]; then AUTH=(-H "X-API-Key: ${KB_API_KEY}"); fi` の条件付きパターンを使用（KB_API_KEY 未設定時はヘッダなしで動作する）。
