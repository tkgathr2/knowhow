-- コストカッターくん（token-cutter）の発動イベント記録テーブル。
-- PreToolUse ゲートが「重い手」を検知して助言したイベントを貯め、実績を可視化する。
-- 起動時マイグレーション(_run_migrations)で冪等に適用される（IF NOT EXISTS）。
CREATE TABLE IF NOT EXISTS kb_token_cutter_events (
    id           BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    pc           TEXT,
    tool         TEXT NOT NULL,        -- Read | Grep
    reason       TEXT NOT NULL,        -- large_read | broad_grep | other
    target_kb    INTEGER,              -- 対象ファイルサイズ(KB)。Grep等は NULL
    est_tokens   INTEGER NOT NULL DEFAULT 0,  -- 避けられた推定トークン
    meta         JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_tc_events_occurred ON kb_token_cutter_events (occurred_at);
