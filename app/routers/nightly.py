"""Self-growth engine — Phase A: nightly heartbeat (minimal compounding core).

育つAI v5 の「夜1パス」を、SREの定石（冪等・advisory lock・catch-up）で
取りこぼしなく回す最小核。1晩で:
  1) decay      … 古く未想起のchunkの信頼度を減衰（既存 intelligence.decay と同ロジック）
  2) recurrence … 北極星「再発した既知ミス件数」を集計（低いほど良い）
  3) digest     … 朝サマリ(jsonb)を生成して kb_nightly_run に保存

すべて追加のみ・後方互換。フロー/ルール変更・本番反映・PII・課金は自動でやらない
（提案は digest に載せるだけ。実行は人間承認）。

注意（SRE指摘）: pg の session advisory lock は PgBouncer の transaction pooling 下では
予期せず解放されうる。Railway内蔵Postgresへ直結している前提。プーラ経由にする場合は
ロック取得用に専用の非プーリング接続を用意すること。
"""

import json
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(tags=["nightly"])

# 夜間ランの多重起動を1本化するための固定 advisory lock キー（任意の定数）。
_NIGHTLY_LOCK_KEY = 982451653


class NightlyRunRequest(BaseModel):
    run_date: date | None = None  # 省略時は今日(UTC)。catch-up対象も自動判定
    catchup_days: int = Field(default=7, ge=1, le=31)  # 未完了の過去N日を遡って埋める
    days_threshold: int = Field(default=90, ge=1)  # decay: 何日未想起で対象か
    decay_factor: float = Field(default=0.95, gt=0, le=1)
    min_confidence: float = Field(default=0.1, ge=0, le=1)
    recurrence_window_days: int = Field(default=1, ge=1, le=30)  # 再発判定の対象期間
    dry_run: bool = False


class NightlyDigest(BaseModel):
    run_date: str
    status: str
    decayed_count: int
    recurrence_count: int  # 北極星
    scored_count: int
    fail_session_count: int
    note: str


class NightlyRunResponse(BaseModel):
    processed: list[NightlyDigest]
    north_star_latest: int | None
    dry_run: bool
    message: str


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


async def _decay(db: AsyncSession, cutoff: datetime, decay_factor: float, min_conf: float, dry_run: bool) -> int:
    """古く未想起のchunkの信頼度を係数で減衰（下限ガード）。既存 intelligence.decay と同等。"""
    where = (
        "is_deprecated = false AND confidence_score > :min_conf "
        "AND (last_recalled_at IS NULL OR last_recalled_at < :cutoff) "
        "AND created_at < :cutoff"
    )
    params = {"min_conf": min_conf, "cutoff": cutoff}
    count_row = await db.execute(text(f"SELECT count(*) FROM kb_chunks WHERE {where}"), params)
    affected = int(count_row.scalar() or 0)
    if not dry_run and affected > 0:
        await db.execute(
            text(
                f"UPDATE kb_chunks SET confidence_score = greatest(:min_conf, confidence_score * :decay_factor) "
                f"WHERE {where}"
            ),
            {**params, "decay_factor": decay_factor},
        )
    return affected


async def _recurrence_count(db: AsyncSession, day_start: datetime, window_days: int) -> tuple[int, int]:
    """北極星: 「既知ミスの再発件数」。
    判定(v1ヒューリスティック): 対象期間に status='fail' のセッションのうち、
    その日より前に作られた『既知の失敗知見』(chunk_type in anti_pattern/error, 非deprecated)と
    タグが1つ以上重なるものを「再発」とみなして数える。
    戻り値: (recurrence_count=再発したfailセッション数, fail_session_count=対象fail総数)
    """
    window_start = day_start - timedelta(days=window_days - 1)
    window_end = day_start + timedelta(days=1)
    params = {"ws": window_start, "we": window_end}

    total_row = await db.execute(
        text(
            "SELECT count(*) FROM kb_sessions "
            "WHERE status = 'fail' AND created_at >= :ws AND created_at < :we"
        ),
        params,
    )
    fail_total = int(total_row.scalar() or 0)

    rec_row = await db.execute(
        text(
            "SELECT count(DISTINCT s.id) FROM kb_sessions s "
            "WHERE s.status = 'fail' AND s.created_at >= :ws AND s.created_at < :we "
            "AND coalesce(array_length(s.tags, 1), 0) > 0 "
            "AND EXISTS ( "
            "  SELECT 1 FROM kb_chunks c "
            "  WHERE c.is_deprecated = false "
            "    AND c.chunk_type IN ('anti_pattern', 'error') "
            "    AND c.created_at < s.created_at "
            "    AND c.tags && s.tags "
            ")"
        ),
        params,
    )
    recurrence = int(rec_row.scalar() or 0)
    return recurrence, fail_total


async def _scored_count(db: AsyncSession, day_start: datetime) -> int:
    """採点燃料: その日のrecallログ件数（参照された＝採点対象）。"""
    _, day_end = _day_bounds(day_start.date())
    row = await db.execute(
        text("SELECT count(*) FROM kb_recall_log WHERE created_at >= :ds AND created_at < :de"),
        {"ds": day_start, "de": day_end},
    )
    return int(row.scalar() or 0)


async def _dates_to_process(db: AsyncSession, today: date, catchup_days: int) -> list[date]:
    """過去 catchup_days 日（今日含む）で status='done' でない日を、古い順に返す。"""
    start = today - timedelta(days=catchup_days - 1)
    done_rows = await db.execute(
        text("SELECT run_date FROM kb_nightly_run WHERE status = 'done' AND run_date >= :start"),
        {"start": start},
    )
    done = {r[0] for r in done_rows}
    out = []
    d = start
    while d <= today:
        if d not in done:
            out.append(d)
        d += timedelta(days=1)
    return out


@router.post("/nightly/run", response_model=NightlyRunResponse)
async def nightly_run(req: NightlyRunRequest, db: AsyncSession = Depends(get_db)) -> NightlyRunResponse:
    # 多重起動防止: advisory lock を試行（取れなければ別ランが進行中）
    lock_row = await db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _NIGHTLY_LOCK_KEY})
    if not bool(lock_row.scalar()):
        raise HTTPException(status_code=409, detail="Another nightly run is in progress")

    processed: list[NightlyDigest] = []
    try:
        if req.run_date is not None:
            targets = [req.run_date]
        else:
            today = datetime.now(UTC).date()
            targets = await _dates_to_process(db, today, req.catchup_days)

        if not targets:
            return NightlyRunResponse(
                processed=[], north_star_latest=None, dry_run=req.dry_run,
                message="処理対象なし（本日分は完了済み）",
            )

        # decay は「現在状態」操作。catch-up で複数日を処理しても多重減衰しないよう1回だけ実行し、
        # 最新日(latest)の digest に計上する（過去の遡り分は decayed=0）。
        cutoff = datetime.now(UTC) - timedelta(days=req.days_threshold)
        latest = targets[-1]
        decayed_total = await _decay(db, cutoff, req.decay_factor, req.min_confidence, req.dry_run)

        for rd in targets:
            day_start, _ = _day_bounds(rd)
            decayed = decayed_total if rd == latest else 0

            if not req.dry_run:
                await db.execute(
                    text(
                        "INSERT INTO kb_nightly_run (run_date, status, started_at) "
                        "VALUES (:rd, 'running', now()) "
                        "ON CONFLICT (run_date) DO UPDATE SET status='running', started_at=now(), error=NULL"
                    ),
                    {"rd": rd},
                )

            recurrence, fail_total = await _recurrence_count(db, day_start, req.recurrence_window_days)
            scored = await _scored_count(db, day_start)

            note = (
                f"decay {decayed}件 / 再発(北極星) {recurrence}件 / fail {fail_total}件 / 採点 {scored}件"
                + ("（dry-run）" if req.dry_run else "")
            )
            digest_obj = {
                "run_date": rd.isoformat(),
                "decayed_count": decayed,
                "recurrence_count": recurrence,
                "fail_session_count": fail_total,
                "scored_count": scored,
                "north_star": "recurrence_count（再発した既知ミス件数・低いほど良い）",
                "note": note,
                "generated_at": datetime.now(UTC).isoformat(),
            }

            if not req.dry_run:
                await db.execute(
                    text(
                        "UPDATE kb_nightly_run SET status='done', decayed_count=:d, recurrence_count=:r, "
                        "scored_count=:s, fail_session_count=:f, digest=cast(:digest AS jsonb), finished_at=now() "
                        "WHERE run_date=:rd"
                    ),
                    {
                        "d": decayed,
                        "r": recurrence,
                        "s": scored,
                        "f": fail_total,
                        "rd": rd,
                        "digest": json.dumps(digest_obj, ensure_ascii=False),
                    },
                )

            processed.append(
                NightlyDigest(
                    run_date=rd.isoformat(),
                    status="done" if not req.dry_run else "dry_run",
                    decayed_count=decayed,
                    recurrence_count=recurrence,
                    scored_count=scored,
                    fail_session_count=fail_total,
                    note=note,
                )
            )

        if not req.dry_run:
            await db.commit()

        north_star = processed[-1].recurrence_count if processed else None
        return NightlyRunResponse(
            processed=processed,
            north_star_latest=north_star,
            dry_run=req.dry_run,
            message=f"{len(processed)}日分を処理（catch-up含む）",
        )
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _NIGHTLY_LOCK_KEY})
        if not req.dry_run:
            await db.commit()


@router.get("/nightly/latest", response_model=NightlyDigest)
async def nightly_latest(db: AsyncSession = Depends(get_db)) -> NightlyDigest:
    """朝サマリ取得: 直近 done のラン。"""
    row = await db.execute(
        text(
            "SELECT run_date, status, decayed_count, recurrence_count, scored_count, fail_session_count, digest "
            "FROM kb_nightly_run WHERE status='done' ORDER BY run_date DESC LIMIT 1"
        )
    )
    r = row.first()
    if not r:
        raise HTTPException(status_code=404, detail="No completed nightly run yet")
    note = ""
    try:
        d = r.digest
        if isinstance(d, str):
            d = json.loads(d)
        note = (d or {}).get("note", "")
    except Exception:
        note = ""
    return NightlyDigest(
        run_date=r.run_date.isoformat(),
        status=r.status,
        decayed_count=r.decayed_count,
        recurrence_count=r.recurrence_count,
        scored_count=r.scored_count,
        fail_session_count=r.fail_session_count,
        note=note,
    )
