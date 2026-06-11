-- V9: Scoring pipeline — Phase S1 (attribution of session outcomes to recalled chunks)
-- 採点配管: kb_recall_log の各行に scored_at を付与し、セッション結果を α/β に反映する。
-- Idempotent: 何度実行しても安全（IF NOT EXISTS）。追加のみ・後方互換。

-- recall ログに採点済みタイムスタンプ列を追加
ALTER TABLE kb_recall_log ADD COLUMN IF NOT EXISTS scored_at timestamptz;

-- 未採点行の高速スキャン用インデックス（scored_at IS NULL フィルタ付き）
CREATE INDEX IF NOT EXISTS ix_kb_recall_log_unscored
ON kb_recall_log(created_at)
WHERE scored_at IS NULL;
