-- v14: 経営判断シグナル（kb_signals）
-- 秋好モデル③＝録音→まとめの後に「社長が知る/判断すべきこと」だけを LLM が自動抽出して溜める表。
-- ロア（録音資産）の日次パイプラインに 1 ステップ足すだけ。雑談・確定済みは捨て、効くものだけ残す。
-- 冪等: 同一日の再実行は uq_kb_signals_dedup（project_key+signal_date+dedup_hash）で重複を弾く。

BEGIN;

CREATE TABLE IF NOT EXISTS kb_signals (
    id                  BIGSERIAL PRIMARY KEY,
    project_key         TEXT NOT NULL DEFAULT 'lore' REFERENCES kb_projects(project_key) ON DELETE CASCADE,
    signal_date         DATE NOT NULL,
    signal_type         TEXT NOT NULL DEFAULT 'other',
    title               TEXT NOT NULL,
    detail              TEXT,
    who                 TEXT,
    importance          INTEGER NOT NULL DEFAULT 5,
    status              TEXT NOT NULL DEFAULT 'open',
    source_recording_id BIGINT,
    dedup_hash          TEXT NOT NULL,
    meta                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_signals_dedup
    ON kb_signals (project_key, signal_date, dedup_hash);
CREATE INDEX IF NOT EXISTS ix_kb_signals_date       ON kb_signals (signal_date);
CREATE INDEX IF NOT EXISTS ix_kb_signals_type       ON kb_signals (signal_type);
CREATE INDEX IF NOT EXISTS ix_kb_signals_status     ON kb_signals (status);
CREATE INDEX IF NOT EXISTS ix_kb_signals_importance ON kb_signals (importance);

COMMIT;
