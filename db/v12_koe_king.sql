-- v12: こえキング（録音資産化）Phase 0
-- Plaud録音を構造化して蓄積する基盤。追加のみ・既存無改変。
-- - kb_recordings : 録音1件=1行（watermark台帳・冪等キー=plaud_id）
-- - kb_utterances : 話者別・ミリ秒精度の発話原文（Phase1〜3全機能の原材料）
-- - kb_speaker_aliases : 話者ラベル揺れの正規化表
-- 検索資産は既存 kb_chunks に相乗り（project_key='koe-king', source_type='recording'）。
-- kb_sessions / 夜間採点には一切触れない（開発KPI汚染防止）。

BEGIN;

-- 録音レジストリ
CREATE TABLE IF NOT EXISTS kb_recordings (
  id bigserial PRIMARY KEY,
  plaud_id text NOT NULL UNIQUE,            -- 冪等キー（再送安全）
  title text,
  recorded_at timestamptz,
  duration_minutes int,
  transcript_status text NOT NULL DEFAULT 'pending',
    -- pending(未生成) | ingested(取込済) | empty(無音=セグメント0) | failed
  speaker_set text[] NOT NULL DEFAULT '{}', -- 正規化後の登場人物
  meta jsonb NOT NULL DEFAULT '{}'::jsonb,  -- 原title・話者対応表など
  ingested_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_kb_recordings_recorded_at
ON kb_recordings(recorded_at DESC);

CREATE INDEX IF NOT EXISTS ix_kb_recordings_status
ON kb_recordings(transcript_status);

-- 発話（話者別・ms精度の原文）
CREATE TABLE IF NOT EXISTS kb_utterances (
  id bigserial PRIMARY KEY,
  recording_id bigint NOT NULL REFERENCES kb_recordings(id) ON DELETE CASCADE,
  seq int NOT NULL,
  speaker text NOT NULL,                    -- 正規化後（例: 高木豊大）
  speaker_raw text,                         -- Plaud原ラベル（例: Speaker 2 / Atsuhiro Takagi）
  start_ms bigint NOT NULL,
  end_ms bigint NOT NULL,
  content text NOT NULL,
  UNIQUE (recording_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_kb_utterances_speaker
ON kb_utterances(speaker);

-- 話者エイリアス（「Atsuhiro Takagi」「髙木豊大」→「高木豊大」）
CREATE TABLE IF NOT EXISTS kb_speaker_aliases (
  alias text PRIMARY KEY,
  canonical text NOT NULL
);

-- koe-king プロジェクトを登録（録音内容を他プロジェクト検索に漏らさない）
INSERT INTO kb_projects (project_key, display_name, allow_cross_project_search)
VALUES ('koe-king', 'こえキング（録音資産）', false)
ON CONFLICT (project_key) DO NOTHING;

COMMIT;
