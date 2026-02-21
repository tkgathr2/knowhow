# ノウハウキング君 (Knowledge King) 完全仕様書

## 概要

ノウハウキング君は、AI開発エージェント（Devin, Claude Code, Cursor等）のための**外部記憶基盤**です。
開発セッションで得た知見・エラー解決法・設計判断を蓄積し、次回のセッションで自動的に過去の知見を呼び出すことで、AIの学習を跨ぎセッション的に蓄積・活用します。

- **本番URL**: `https://knowhow.up.railway.app`
- **技術スタック**: FastAPI (Python) + PostgreSQL + pgvector + OpenAI Embeddings
- **ホスティング**: Railway
- **ベクトル次元数**: 1536 (text-embedding-3-large)

---

## アーキテクチャ

```
AIエージェント (Devin等)
    │
    ├── セッション開始時 → POST /api/devin/recall （過去知見を検索）
    │                        ↓
    │                   [ベクトル検索 + 全文検索 + ILIKEフォールバック]
    │                        ↓
    │                   関連する過去の知見を返却
    │
    ├── セッション中 → POST /api/search （任意のタイミングで検索）
    │
    └── セッション終了時 → POST /api/devin/memorize （学びを記録）
                              ↓
                         [SHA256重複チェック → Session作成 → Chunk作成 → Embedding生成]
```

---

## データモデル

### kb_projects（プロジェクトマスタ）
| カラム | 型 | 説明 |
|--------|-----|------|
| project_key | text (PK) | プロジェクト識別子（リポジトリ名等） |
| display_name | text | 表示名 |
| allow_cross_project_search | boolean | 横断検索許可（default: false） |
| embedding_model | text | 使用するembeddingモデル（default: text-embedding-3-large） |
| embedding_dimensions | int | ベクトル次元数（default: 1536） |
| search_confidence_threshold | float | 検索の最低信頼度スコア（default: 0.70） |
| recency_half_life_days | int | 鮮度半減期（default: 90日） |

### kb_sessions（セッション履歴）
| カラム | 型 | 説明 |
|--------|-----|------|
| id | bigserial (PK) | セッションID |
| project_key | text (FK) | プロジェクト |
| tool | text | 使用ツール（devin, claude_code, cursor） |
| status | text | 結果（success, fail, partial） |
| environment | text | 環境（local, prod） |
| raw_log | text | 生ログ |
| normalized_log | text | 正規化ログ |
| tags | text[] | タグ配列 |
| hash | text | SHA256ハッシュ（重複防止） |
| ingest_state | text | 処理状態（queued → summarized → embedded） |

### kb_chunks（知識チャンク）
知識の最小単位。セッションログ・外部データ等をチャンク化して格納。
| カラム | 型 | 説明 |
|--------|-----|------|
| id | bigserial (PK) | チャンクID |
| project_key | text (FK) | プロジェクト |
| source_type | text | ソース種別（session, document, external） |
| source_id | bigint | ソースID |
| chunk_type | text | チャンク種別（session_log, github_issue, stackoverflow, vulnerability等） |
| content | text | チャンク本文 |
| embedding | vector(1536) | ベクトル埋め込み |
| importance_score | int | 重要度スコア（0-10） |
| confidence_score | float | 信頼度スコア（ベイズ推定、0.0-1.0） |
| alpha / beta | float | ベイズパラメータ（Beta分布） |
| helpful_count | int | 役立った回数 |
| unhelpful_count | int | 役立たなかった回数 |
| recall_count | int | 呼び出された回数 |
| last_recalled_at | timestamptz | 最後に呼び出された日時 |
| tags | text[] | タグ配列 |
| meta | jsonb | メタデータ |
| search_vector | tsvector | 全文検索ベクトル（トリガーで自動更新） |
| is_deprecated | boolean | 非推奨フラグ |

### kb_feedback（フィードバック）
検索結果に対するフィードバック。信頼度スコアの自動更新に使用。

### kb_recall_log（検索ログ）
recall APIの呼び出し履歴。検索パフォーマンス分析に使用。

### kb_external_sources（外部ソース）
GitHub Issues、Stack Overflow、脆弱性情報等の外部データソース管理。

### kb_issues（問題報告）
チャンクに対する問題報告（stale, wrong, env_mismatch, incomplete）。

---

## API エンドポイント一覧

### コア API（Devin連動）

#### `POST /api/devin/recall` - 知見検索（セッション開始時）
```json
// Request
{
  "project_key": "knowhow",
  "query": "Railway deploy PostgreSQL connection error",
  "top_k": 5
}
// Response
{
  "results": [
    {
      "chunk_id": 42,
      "content": "Railwayデプロイ時にDATABASE_URLが...",
      "chunk_type": "session_log",
      "score": 0.87,
      "tags": ["railway", "deploy", "postgresql"]
    }
  ],
  "query": "...",
  "total": 3,
  "project_key": "knowhow"
}
```
**検索ロジック（3段フォールバック）**:
1. **ベクトル検索**: OpenAI Embeddingでクエリをベクトル化 → pgvector HNSW cosine距離で類似検索
2. **全文検索**: PostgreSQL tsvector (simple辞書) で全文検索
3. **ILIKEフォールバック**: 上記2つで結果が0件の場合、ILIKE部分一致検索（スコア×0.8）

recall実行時、返されたチャンクの `recall_count` と `last_recalled_at` が自動更新される。

#### `POST /api/devin/memorize` - 知見記録（セッション終了時）
```json
// Request
{
  "project_key": "knowhow",
  "tool": "devin",
  "status": "success",
  "environment": "local",
  "raw_log": "Railway PostgreSQLの接続問題を解決。DATABASE_URLにsslmode=requireを追加する必要があった。",
  "tags": ["railway", "postgresql", "ssl"]
}
// Response
{
  "session_id": 123,
  "chunk_id": 456,
  "message": "Memorized"
}
```
**処理フロー**: SHA256ハッシュで重複チェック → Session作成 → Chunk作成 → OpenAI Embedding自動生成

#### `POST /api/devin/bulk-memorize` - 一括記録
複数エントリを一括で記録。過去セッションの一括取り込み等に使用。
```json
{
  "entries": [
    {"project_key": "repo1", "raw_log": "...", "tags": ["tag1"]},
    {"project_key": "repo2", "raw_log": "...", "tags": ["tag2"]}
  ]
}
```

### 検索 API

#### `POST /api/search` - プロジェクト内検索
```json
{"project_key": "knowhow", "query": "deploy error", "top_k": 10, "threshold": 0.5}
```

#### `POST /api/search/cross-project` - 横断検索
全プロジェクトを横断して知見を検索。
```json
{"query": "Docker build failed", "top_k": 10}
```

### フィードバック API

#### `POST /api/feedback` - 検索結果へのフィードバック
```json
{
  "project_key": "knowhow",
  "session_id": 123,
  "query": "deploy error",
  "returned_chunk_ids": [1, 2, 3],
  "selected_chunk_ids": [1],
  "resolved": true,
  "was_helpful": "helpful"  // helpful | partial | unhelpful
}
```
フィードバックにより、チャンクの `alpha`/`beta` パラメータが更新され、`confidence_score` がベイズ推定で自動調整される。

#### `POST /api/issues` - 問題報告
```json
{"project_key": "knowhow", "chunk_id": 42, "reason": "stale"}
```
reason: stale（古い）, wrong（間違い）, env_mismatch（環境不一致）, incomplete（不完全）

#### `POST /api/chunks/deprecate` - チャンク非推奨化
```json
{"project_key": "knowhow", "chunk_id": 42, "is_deprecated": true}
```

### 知能強化 API（Intelligence）

#### `POST /api/intelligence/decay` - 自動信頼度減衰
長期間使われていないチャンクの信頼度スコアを自動的に下げる。
```json
{"days_threshold": 90, "decay_factor": 0.95, "min_confidence": 0.1, "dry_run": false}
```

#### `GET /api/intelligence/duplicates` - 重複検出
ベクトル類似度が高いチャンクペアを検出。
```
GET /api/intelligence/duplicates?threshold=0.95&limit=20&project_key=knowhow
```

#### `POST /api/intelligence/merge-duplicates` - 重複統合
```json
{"keep_chunk_id": 1, "remove_chunk_id": 2}
```
統計値（helpful_count, recall_count等）を合算し、重複チャンクを非推奨化。

#### `POST /api/intelligence/summary` - AI要約生成
GPT-4o-miniを使用してプロジェクトの知見をAI要約。
```json
{"project_key": "knowhow", "top_k": 20}
```

#### `GET /api/intelligence/recall-stats` - 検索統計
```
GET /api/intelligence/recall-stats?days=30
```

#### `GET /api/intelligence/top-chunks` - トップチャンク
```
GET /api/intelligence/top-chunks?sort_by=recall_count&limit=20
```

### 外部データ取り込み API

#### `POST /api/external/github-issues` - GitHub Issues取り込み
```json
{"repo": "tkgathr2/knowhow", "project_key": "knowhow", "state": "closed", "max_issues": 20}
```

#### `POST /api/external/stackoverflow` - Stack Overflow取り込み
```json
{"query": "fastapi deploy railway", "project_key": "knowhow", "tagged": "python,fastapi", "max_results": 10}
```

#### `POST /api/external/audit` - 脆弱性情報取り込み
```json
{"project_key": "knowhow", "audit_type": "npm_audit", "vulnerabilities": [{"name": "lodash", "severity": "high", "title": "..."}]}
```

#### `GET /api/external/sources` / `POST /api/external/sources` - 外部ソース管理

### ダッシュボード API

#### `GET /api/stats` - 全体統計
プロジェクト数、セッション数、チャンク数、埋め込み済み数

#### `GET /api/recent` - 最近のセッション一覧
```
GET /api/recent?limit=20&offset=0&project_key=knowhow&tag=deploy
```

#### `GET /api/tags` - タグ統計

#### `GET /api/chunks/{chunk_id}` - チャンク詳細

#### `GET /health` - ヘルスチェック

### ダッシュボード UI
`https://knowhow.up.railway.app/` にアクセスすると、ブラウザベースのダッシュボードUIが表示される。統計、セッション一覧、横断検索、タグクラウド等を閲覧可能。

---

## 信頼度スコアの仕組み（ベイズ推定）

チャンクの品質はBeta分布によるベイズ推定で管理される。

```
confidence_score = alpha / (alpha + beta)
```

- 初期値: alpha=9.0, beta=1.0 → confidence=0.9
- helpful フィードバック → alpha += 1.0（スコア上昇）
- unhelpful フィードバック → beta += 1.0（スコア下降）
- partial フィードバック → 選択されたチャンクは alpha += 0.5、未選択は beta += 0.5
- 自動減衰: 90日以上recallされないチャンクは confidence × 0.95

検索時、`confidence_score` がプロジェクトの `search_confidence_threshold`（default: 0.70）未満のチャンクは結果から除外される。

---

## .devin/rules による自動連動

各リポジトリの `.devin/rules` ファイルに以下を記述することで、Devinセッション開始時に自動的にノウハウキング君と連動する。

### セッション開始時の自動recall
```bash
curl -s -X POST https://knowhow.up.railway.app/api/devin/recall \
  -H 'Content-Type: application/json' \
  -d '{"project_key":"<リポジトリ名>","query":"<タスクの要約>","top_k":5}'
```

### DEVIN運用ルール（憲法）の自動読み込み
```bash
curl -s -X POST https://knowhow.up.railway.app/api/devin/recall \
  -H 'Content-Type: application/json' \
  -d '{"project_key":"DEVIN_CONSTITUTION","query":"DEVIN運用ルール 憲法","top_k":5}'
```

### セッション終了時の自動memorize
```bash
curl -s -X POST https://knowhow.up.railway.app/api/devin/memorize \
  -H 'Content-Type: application/json' \
  -d '{"project_key":"<リポジトリ名>","tool":"devin","status":"success","environment":"local","raw_log":"<学んだこと>","tags":["<タグ>"]}'
```

---

## 環境変数

| 変数名 | 説明 | 必須 |
|--------|------|------|
| DATABASE_URL | PostgreSQL接続URL（asyncpg形式） | Yes |
| OPENAI_API_KEY | OpenAI APIキー（Embedding生成用） | Yes（ベクトル検索に必要） |
| KB_API_KEY | APIキー（将来の認証用、現在未使用） | No |

---

## 現在の統計（2026年2月時点）

- 登録プロジェクト数: 17
- 総セッション数: 456
- 総チャンク数: 608（全て埋め込み済み）
- 外部ソース数: 28（GitHub PRs, Stack Overflow, npm audit）
- 適用リポジトリ数: 33（全てに .devin/rules 設定済み）
