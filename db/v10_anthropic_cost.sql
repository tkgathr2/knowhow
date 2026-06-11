-- Anthropic費用ダッシュボード：Gmailに届く領収書（invoice+statements@mail.anthropic.com）を
-- 毎日取り込み、月次累計・日別・種別をUSD+JPYで可視化するためのテーブル。
-- 起動時マイグレーション(_run_migrations)で冪等に適用される（IF NOT EXISTS）。
CREATE TABLE IF NOT EXISTS kb_anthropic_receipts (
    id            BIGSERIAL PRIMARY KEY,
    receipt_no    TEXT NOT NULL UNIQUE,    -- 領収書番号（例 2949-8653-8225）＝冪等キー
    receipt_date  DATE NOT NULL,           -- 支払日（JST）
    description   TEXT NOT NULL,           -- 明細（例 Auto recharge extra usage, Individual plan）
    kind          TEXT NOT NULL,           -- api_credit | extra_usage | subscription | other
    subtotal_usd  DOUBLE PRECISION NOT NULL,
    tax_usd       DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_usd     DOUBLE PRECISION NOT NULL,
    usdjpy        DOUBLE PRECISION,        -- 取込時点のUSD/JPYレート
    total_jpy     INTEGER,                 -- total_usd * usdjpy（取込時点換算）
    meta          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_anthropic_receipts_date ON kb_anthropic_receipts (receipt_date);
