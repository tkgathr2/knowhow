"""らんさ〜ず（秋好陽介・ランサーズ創業者）YouTube全動画の知見を knowhow に取り込むシード。

社長依頼（2026-06-14）: らんさ〜ずの解析（104本の要点＋テーマ＋AI活用まとめ）を全部ノウハウキングに入れる。
データは app/data/ranraners.json（リポジトリ同梱）。起動時にバックグラウンドで一度だけ冪等に取り込む。

設計方針:
- 起動をブロックしない（lifespan から create_task で起動。呼び出し側で強参照を保持しGC回避）。
- 冪等: project="ranraners" のチャンク数が「raw_logが空でないエントリ数」以上なら何もしない（再起動で再実行しない）。
  未完了なら raw_log の sha256 ハッシュで個別重複スキップしつつ穴埋め（bulk-memorize と同方式）。
- 部分耐性: 20件ごとに commit（途中失敗でも既取り込み分は残る／長時間トランザクション・接続占有を回避）。
- 競合耐性: hash の一意制約（uq_kb_sessions_hash）違反は savepoint で握って当該のみスキップ（多重起動レース対策）。
- 失敗しても本体に影響させない（全例外を握る・ログのみ）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session
from app.embedding import create_embedding
from app.models import KbChunk, KbProject, KbSession

_logger = logging.getLogger(__name__)
_DATA_PATH = Path(__file__).parent / "data" / "ranraners.json"
_COMMIT_EVERY = 20


async def maybe_seed_ranraners() -> None:
    """app/data/ranraners.json を冪等に取り込む。起動時にバックグラウンドで呼ぶ。"""
    try:
        if not _DATA_PATH.exists():
            _logger.info("ranraners seed: data file not found, skip")
            return
        data = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        project_key = data.get("project_key", "ranraners")
        display_name = data.get("display_name", project_key)
        source = data.get("source", "youtube:@らんさーず")
        entries = [e for e in data.get("entries", []) if (e.get("raw_log") or "").strip()]
        if not entries:
            return

        async with async_session() as db:
            # 冪等: 既に「実エントリ数」以上のチャンクがあれば完了済みとみなしスキップ
            count = await db.scalar(
                select(func.count(KbChunk.id)).where(KbChunk.project_key == project_key)
            )
            if count is not None and count >= len(entries):
                _logger.info(
                    "ranraners seed: already seeded (%s chunks >= %s entries), skip",
                    count,
                    len(entries),
                )
                return

            project = await db.scalar(
                select(KbProject).where(KbProject.project_key == project_key)
            )
            if not project:
                db.add(KbProject(project_key=project_key, display_name=display_name))
                await db.flush()

            imported = 0
            skipped = 0
            for entry in entries:
                raw_log = entry["raw_log"].strip()
                tags = entry.get("tags", [])
                meta = entry.get("meta", {})
                log_hash = hashlib.sha256(raw_log.encode("utf-8")).hexdigest()

                exists = await db.scalar(
                    select(KbSession.id).where(
                        KbSession.project_key == project_key,
                        KbSession.hash == log_hash,
                    )
                )
                if exists:
                    skipped += 1
                    continue

                session = KbSession(
                    project_key=project_key,
                    tool="youtube",
                    status="success",
                    environment="prod",
                    raw_log=raw_log,
                    normalized_log=raw_log,
                    tags=tags,
                    hash=log_hash,
                    ingest_state="summarized",
                )
                chunk = KbChunk(
                    project_key=project_key,
                    source_type="session",  # 裏に KbSession を作るため session と整合（bulk.py と同様）
                    chunk_type="youtube_knowledge",
                    content=raw_log,
                    importance_score=6,  # 外部知見はやや高importance・やや低confidenceで重み付け
                    confidence_score=0.85,
                    alpha=9.0,
                    beta=1.0,
                    tags=tags,
                    meta={**meta, "source": source, "seed": "ranraners"},
                )
                # embedding は savepoint の外で付与（失敗時も NULL で保存し継続）
                try:
                    embedding = await create_embedding(raw_log)
                    if embedding is not None:
                        chunk.embedding = embedding
                        chunk.embedding_model = settings.embedding_model
                        chunk.embedding_dimensions = settings.embedding_dim
                        session.ingest_state = "embedded"
                    else:
                        session.ingest_state = "failed_embedding"
                except Exception as e:  # noqa: BLE001
                    session.ingest_state = "failed_embedding"
                    _logger.warning("ranraners seed: embedding failed: %s", e)

                try:
                    async with db.begin_nested():  # savepoint: 一意制約レースは当該のみ握る
                        db.add(session)
                        await db.flush()
                        chunk.source_id = session.id
                        db.add(chunk)
                        await db.flush()
                except IntegrityError:
                    skipped += 1
                    continue

                imported += 1
                if imported % _COMMIT_EVERY == 0:
                    await db.commit()

            await db.commit()
            _logger.info(
                "ranraners seed done: imported=%s skipped=%s (total entries=%s)",
                imported,
                skipped,
                len(entries),
            )
    except Exception as e:  # noqa: BLE001 — シード失敗は本体に波及させない
        _logger.warning("ranraners seed error (ignored): %s", e)
