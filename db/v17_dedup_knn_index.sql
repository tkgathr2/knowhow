-- v17: 夜間の重複統合(dedup)を「全ペア自己結合 O(n²)」→「pgvector 近傍(KNN)検索」へ置換した
--      ことに伴う索引整備。
--
-- 背景: app/routers/nightly.py の _merge_duplicate_chunks を、
--   旧) project内の全ペア × (embedding <=> embedding)  ＝ O(n²)・データ増で無限実行 → 2026-06-30 本番全断
--   新) 直近N日に作成された候補chunkを最大M件だけ走査し、各々 HNSW索引で「ORDER BY embedding <=> :vec
--       LIMIT k」の近傍検索(LATERAL)で類似ペアを引く  ＝ 走査量を候補数×k に上限化
--   に置換した。これを高速に効かせるため (a) embedding近傍索引(HNSW) と
--   (b) 候補chunk(直近作成・非deprecated・embeddingあり)の絞り込み部分索引 を保証する。
--
-- 冪等: 全て IF NOT EXISTS。schema.sql で HNSW を作成済みの既存環境では (a) は即時 no-op。
--
-- ロック注意: HNSW を「新規に」張る必要がある環境では本マイグレーションがブロックしうる
--   （2万行規模で 60s 超の恐れ → 起動マイグレーションの statement_timeout=60s で打ち切り skip）。
--   本番(schema.sql 由来でHNSW在り)では no-op のため問題なし。万一 HNSW が欠落した環境では、
--   トランザクション外・無停止で次を別経路で手動適用すること:
--     CREATE INDEX CONCURRENTLY ix_kb_chunks_embedding_hnsw
--       ON kb_chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

BEGIN;

-- (a) embedding 近傍索引(cosine)。schema.sql と同一定義。既存なら no-op。
CREATE INDEX IF NOT EXISTS ix_kb_chunks_embedding_hnsw
    ON kb_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- (b) 候補chunkの絞り込み（直近作成・非deprecated・embeddingあり）を効かせる部分索引。
--     dedup候補SELECT( WHERE is_deprecated=false AND embedding IS NOT NULL AND created_at>=cutoff
--     ORDER BY created_at DESC LIMIT n )をインデックスで賄う。btree・小さく安全。
CREATE INDEX IF NOT EXISTS ix_kb_chunks_dedup_candidates
    ON kb_chunks (created_at DESC)
    WHERE is_deprecated = false AND embedding IS NOT NULL;

COMMIT;
