-- v16: 「成長の毎日ログ」(/daily) 高速化用インデックス
-- 症状: /api/growth/daily が初回1.5〜2.3秒（キャッシュ後は0.25秒）。
-- 原因: kb_chunks には (project_key, created_at) 複合しか無く、project_key 指定なしの
--       created_at だけの範囲・日次GROUP BYがテーブル全体（webhookログ含む）をフルスキャンしていた。
-- 対策: created_at 単独 / (created_at, source_type) / last_recalled_at にbtreeを張り、
--       範囲スキャン＋日次集計をインデックスで賄えるようにする（データ増でも遅くならない）。
-- 冪等: IF NOT EXISTS。挙動は変えない（クエリ結果は同一・速度のみ改善）。

BEGIN;

-- created_at 範囲スキャン（日次GROUP BY・前日累計 base_before・items の order by/limit）
CREATE INDEX IF NOT EXISTS ix_kb_chunks_created
    ON kb_chunks (created_at);

-- 資産/自動記録の条件集計を範囲＋種別で効かせる（source_type FILTER 付きGROUP BY）
CREATE INDEX IF NOT EXISTS ix_kb_chunks_created_source
    ON kb_chunks (created_at, source_type);

-- 「使われた知恵（想起）」の last_recalled_at 範囲集計用（NULLは除外）
CREATE INDEX IF NOT EXISTS ix_kb_chunks_recalled
    ON kb_chunks (last_recalled_at)
    WHERE last_recalled_at IS NOT NULL;

COMMIT;
