-- V7: Self-growth engine — Phase A (nightly heartbeat, minimal compounding core)
-- 育つAI v5 の「夜1パス」を冪等・取りこぼし防止で回すための進捗/サマリテーブル。
-- Idempotent: 何度実行しても安全（IF NOT EXISTS）。追加のみ・後方互換。

-- 夜間ラン（run_date を冪等キーにする）。1晩=1行。
-- SRE設計：run_date 主キーで「その日のランが完了済みか」を判定し、二重実行を防ぐ。
CREATE TABLE IF NOT EXISTS kb_nightly_run (
  run_date date PRIMARY KEY,
  status text NOT NULL DEFAULT 'running',   -- running | done | failed
  decayed_count int NOT NULL DEFAULT 0,     -- decay対象になったchunk数
  recurrence_count int NOT NULL DEFAULT 0,  -- 北極星：再発した既知ミス件数（低いほど良い）
  scored_count int NOT NULL DEFAULT 0,      -- 採点対象（参照ログ）件数
  fail_session_count int NOT NULL DEFAULT 0,
  digest jsonb NOT NULL DEFAULT '{}'::jsonb, -- 朝サマリ本体
  error text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS ix_kb_nightly_run_started
ON kb_nightly_run(started_at DESC);

CREATE INDEX IF NOT EXISTS ix_kb_nightly_run_status
ON kb_nightly_run(status, run_date DESC);
