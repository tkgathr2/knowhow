-- V6: Intelligence features (recall tracking, external sources, decay support)
-- Idempotent: safe to run multiple times (IF NOT EXISTS everywhere)

-- Add recall tracking columns to kb_chunks
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS last_recalled_at timestamptz;
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS recall_count int NOT NULL DEFAULT 0;

-- Recall log: every recall query + returned chunks
CREATE TABLE IF NOT EXISTS kb_recall_log (
  id bigserial PRIMARY KEY,
  project_key text NOT NULL REFERENCES kb_projects(project_key) ON DELETE CASCADE,
  query text NOT NULL,
  returned_chunk_ids bigint[] NOT NULL DEFAULT '{}',
  top_score double precision,
  result_count int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_kb_recall_log_project_created
ON kb_recall_log(project_key, created_at DESC);

-- External knowledge sources configuration
CREATE TABLE IF NOT EXISTS kb_external_sources (
  id bigserial PRIMARY KEY,
  source_type text NOT NULL,  -- github_issues, stackoverflow, npm_audit, pypi_audit, docs
  source_url text NOT NULL,
  project_key text REFERENCES kb_projects(project_key) ON DELETE SET NULL,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_synced_at timestamptz,
  sync_count int NOT NULL DEFAULT 0,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_external_sources_type_url
ON kb_external_sources(source_type, source_url);
