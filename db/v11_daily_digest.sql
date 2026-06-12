-- v11: 1日のダイジェスト（素人向け日本語まとめ）の保存先。
-- /daily で「全部読まなくても1日が3〜5行でわかる」ためのキャッシュ。
CREATE TABLE IF NOT EXISTS kb_daily_digest (
    digest_date DATE PRIMARY KEY,
    headline    TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    stats       JSONB NOT NULL DEFAULT '{}',
    model       TEXT NOT NULL DEFAULT '',
    is_final    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
