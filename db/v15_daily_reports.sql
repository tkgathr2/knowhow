-- v15: 各部署の日次レポート（daily_reports）
-- 日報の表示の本拠を Notion→knowhow（Web）に移すための表。まずステップアップ（'stepup'）から。
-- 将来 総務（'soumu'）・交通誘導（'koutsu'）も同じ枠で増やせる。1部署×1日で1件。
-- 冪等: 同一(department, report_date)の再投入は uq_daily_reports_dept_date で弾き、ルータ側で UPDATE。

BEGIN;

CREATE TABLE IF NOT EXISTS daily_reports (
    id            BIGSERIAL PRIMARY KEY,
    department    TEXT NOT NULL,
    report_date   DATE NOT NULL,
    bucho         TEXT,
    bucho_comment TEXT,
    title         TEXT,
    summary       TEXT,
    body_md       TEXT,
    metrics       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_reports_dept_date
    ON daily_reports (department, report_date);
CREATE INDEX IF NOT EXISTS ix_daily_reports_date       ON daily_reports (report_date);
CREATE INDEX IF NOT EXISTS ix_daily_reports_department ON daily_reports (department);

COMMIT;
