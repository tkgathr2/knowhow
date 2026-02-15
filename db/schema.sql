-- knowledge-db schema (V5.0 + V4.1 review fixes)
-- Target: PostgreSQL on Railway
-- Vector: pgvector (HNSW), cosine distance
-- Notes:
-- - Embedding dim default: 1536 (text-embedding-3-large w/ dimensions=1536). HNSW limit 2000.
-- - Heavy ingest should be async; DB holds states for recovery.
-- - No secrets stored; raw_log has retention policy.

BEGIN;

-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Helper: updated_at trigger
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Projects master
CREATE TABLE IF NOT EXISTS kb_projects (
  project_key text PRIMARY KEY,
  display_name text,
  allow_cross_project_search boolean NOT NULL DEFAULT false,
  constitution_mode text NOT NULL DEFAULT 'project_only', -- global_plus_project | project_only
  embedding_model text NOT NULL DEFAULT 'text-embedding-3-large',
  embedding_dimensions int NOT NULL DEFAULT 1536,
  search_confidence_threshold double precision NOT NULL DEFAULT 0.70,
  recency_half_life_days int NOT NULL DEFAULT 90,
  constitution_dynamic_top_m int NOT NULL DEFAULT 10,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_kb_projects_updated_at ON kb_projects;
CREATE TRIGGER trg_kb_projects_updated_at
BEFORE UPDATE ON kb_projects
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Documents (constitution/spec/playbook)
CREATE TABLE IF NOT EXISTS kb_documents (
  id bigserial PRIMARY KEY,
  project_key text NOT NULL REFERENCES kb_projects(project_key) ON DELETE CASCADE,
  doc_type text NOT NULL, -- constitution, spec, playbook
  version text NOT NULL,
  title text NOT NULL,
  body text NOT NULL,
  is_latest boolean NOT NULL DEFAULT false,
  checksum text NOT NULL,
  change_log text,
  diff_summary text,
  processing_status text NOT NULL DEFAULT 'ready', -- processing | ready | failed
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_documents_version
ON kb_documents(project_key, doc_type, version);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_documents_latest
ON kb_documents(project_key, doc_type)
WHERE is_latest = true;

DROP TRIGGER IF EXISTS trg_kb_documents_updated_at ON kb_documents;
CREATE TRIGGER trg_kb_documents_updated_at
BEFORE UPDATE ON kb_documents
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Sessions
CREATE TABLE IF NOT EXISTS kb_sessions (
  id bigserial PRIMARY KEY,
  project_key text NOT NULL REFERENCES kb_projects(project_key) ON DELETE CASCADE,
  tool text NOT NULL, -- devin, claude_code, cursor
  status text NOT NULL, -- success, fail, partial
  environment text NOT NULL, -- local, prod
  started_at timestamptz,
  ended_at timestamptz,
  duration_seconds int,
  raw_log text NOT NULL,
  normalized_log text NOT NULL,
  summary_json jsonb,
  summary_text text,
  tags text[] NOT NULL DEFAULT '{}',
  error_count int NOT NULL DEFAULT 0,
  retry_count int NOT NULL DEFAULT 0,
  ingest_state text NOT NULL DEFAULT 'queued', -- queued|processing|summarized|embedded|failed_summary|failed_embedding
  raw_log_retention_until timestamptz,
  hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_sessions_hash
ON kb_sessions(project_key, hash);

CREATE INDEX IF NOT EXISTS ix_kb_sessions_project_created
ON kb_sessions(project_key, created_at DESC);

-- Chunks
-- NOTE: embedding is stored as vector(1536) for simplicity; also store model/dim per row for future coexistence.
CREATE TABLE IF NOT EXISTS kb_chunks (
  id bigserial PRIMARY KEY,
  project_key text NOT NULL REFERENCES kb_projects(project_key) ON DELETE CASCADE,
  source_type text NOT NULL, -- document, session
  source_id bigint NOT NULL,
  chunk_type text NOT NULL, -- rule,error,fix,command,insight,summary,anti_pattern
  content text NOT NULL,
  token_count int,
  importance_score int NOT NULL DEFAULT 5, -- 0..10, normalized in queries by /10
  tags text[] NOT NULL DEFAULT '{}',
  meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  embedding vector(1536),
  embedding_model text NOT NULL DEFAULT 'text-embedding-3-large',
  embedding_dimensions int NOT NULL DEFAULT 1536,
  search_vector tsvector,
  helpful_count int NOT NULL DEFAULT 0,
  unhelpful_count int NOT NULL DEFAULT 0,
  alpha double precision NOT NULL DEFAULT 1.0,
  beta double precision NOT NULL DEFAULT 1.0,
  confidence_score double precision NOT NULL DEFAULT 0.5,
  last_helpful_at timestamptz,
  last_unhelpful_at timestamptz,
  is_deprecated boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_kb_chunks_project_created
ON kb_chunks(project_key, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_kb_chunks_source
ON kb_chunks(source_type, source_id);

CREATE INDEX IF NOT EXISTS ix_kb_chunks_search_vector
ON kb_chunks USING gin (search_vector);

-- HNSW index (cosine)
-- pgvector supports tuning via WITH (...). Use reviewed defaults.
CREATE INDEX IF NOT EXISTS ix_kb_chunks_embedding_hnsw
ON kb_chunks USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- Keep search_vector updated
CREATE OR REPLACE FUNCTION kb_chunks_search_vector_update()
RETURNS trigger AS $$
BEGIN
  NEW.search_vector := setweight(to_tsvector('simple', coalesce(NEW.content,'')), 'A');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_kb_chunks_search_vector ON kb_chunks;
CREATE TRIGGER trg_kb_chunks_search_vector
BEFORE INSERT OR UPDATE OF content ON kb_chunks
FOR EACH ROW EXECUTE FUNCTION kb_chunks_search_vector_update();

-- Feedback (closed loop)
CREATE TABLE IF NOT EXISTS kb_feedback (
  id bigserial PRIMARY KEY,
  project_key text NOT NULL REFERENCES kb_projects(project_key) ON DELETE CASCADE,
  session_id bigint NOT NULL REFERENCES kb_sessions(id) ON DELETE CASCADE,
  query text NOT NULL,
  query_tags text[] NOT NULL DEFAULT '{}',
  returned_chunk_ids bigint[] NOT NULL,
  selected_chunk_ids bigint[] NOT NULL,
  resolved boolean NOT NULL,
  was_helpful text NOT NULL, -- helpful|partial|unhelpful
  resolution_time_seconds int,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_kb_feedback_project_created
ON kb_feedback(project_key, created_at DESC);

-- Issues (negative feedback)
CREATE TABLE IF NOT EXISTS kb_issues (
  id bigserial PRIMARY KEY,
  project_key text NOT NULL REFERENCES kb_projects(project_key) ON DELETE CASCADE,
  chunk_id bigint NOT NULL REFERENCES kb_chunks(id) ON DELETE CASCADE,
  reason text NOT NULL, -- stale, wrong, env_mismatch, incomplete
  status text NOT NULL DEFAULT 'open', -- open, closed
  created_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz
);

CREATE INDEX IF NOT EXISTS ix_kb_issues_project_status
ON kb_issues(project_key, status);

COMMIT;
