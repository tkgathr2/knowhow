-- v13: ロア（Lore・録音資産）プロジェクトの登録
-- プロダクト名を「こえキング」→「ロア（Lore）」に確定（社長決定 2026-06-13）。
-- 録音由来の検索チャンクは project_key='lore' で既存 kb_chunks に相乗りする。
-- allow_cross_project_search=false ＝録音内容を他プロジェクト検索に漏らさない。
-- v12 で登録した 'koe-king' は実チャンク0件のまま無害に残置（履歴保持・参照されない）。

BEGIN;

INSERT INTO kb_projects (project_key, display_name, allow_cross_project_search)
VALUES ('lore', 'ロア（録音資産）', false)
ON CONFLICT (project_key) DO NOTHING;

COMMIT;
