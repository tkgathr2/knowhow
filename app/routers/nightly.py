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
from app.models import KbChunk

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
    dedup_threshold: float = Field(default=0.95, ge=0.8, le=1)  # 重複統合: 類似度しきい値
    dedup_max_pairs: int = Field(default=50, ge=0, le=200)  # 重複統合: 1晩の上限ペア数（0=無効）
    dry_run: bool = False
    # --- Phase S1: 採点パラメータ ---
    score_window_hours: int = Field(default=6, ge=1, le=48)  # recall後この時間以内のセッションを対象
    score_alpha_step: float = Field(default=0.2, gt=0, le=10)  # success時の α 加算量
    score_beta_step: float = Field(default=0.1, gt=0, le=10)   # fail時の β 加算量（冤罪に保守的）
    score_top_n: int = Field(default=3, ge=1, le=20)           # returned_chunk_ids の先頭N件のみ採点
    score_daily_cap: float = Field(default=1.0, gt=0, le=10)   # 1チャンク1晩の変動上限
    score_batch: int = Field(default=500, ge=1, le=5000)       # 1回あたりの処理上限件数
    score_dry_run: bool = True                                  # 既定dry-run（scored_at を打刻しない）


class NightlyDigest(BaseModel):
    run_date: str
    status: str
    decayed_count: int
    recurrence_count: int  # 北極星
    scored_count: int
    fail_session_count: int
    knowledge_gap_count: int = 0  # recall 0件＝知識が無かった回数（次に学ぶべきこと）
    merged_duplicates_count: int = 0  # 重複統合した件数（auto-ingestの近接重複の掃除）
    suggestions: list[str] = []   # 提案バジェット（最大3件・ほとんどの日は0が正常）
    note: str
    # --- Phase S1: 採点メトリクス（後方互換: 既存レコードは 0/False で読まれる） ---
    scoring_processed: int = 0      # 処理した recall ログ件数
    scoring_matched: int = 0        # セッションと突合できた件数
    scoring_alpha_applied: int = 0  # α 加算を実施したチャンク件数
    scoring_beta_applied: int = 0   # β 加算を実施したチャンク件数
    scoring_dry_run: bool = True    # True=dry-run（scored_at 未打刻）
    # --- auto-ingest 品質メトリクス ---
    auto_learn_stored: int = 0      # その日 auto-ingest で取り込まれた件数
    auto_learn_skipped: int = 0     # その日 auto-ingest でスキップされた件数


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


def pick_keeper(id_a: int, conf_a: float, id_b: int, conf_b: float) -> tuple[int, int]:
    """重複ペアのどちらを残すか。信頼度が高い方、同点なら古い方(id小)。戻り値 (keep_id, remove_id)。"""
    if conf_b > conf_a:
        return id_b, id_a
    return id_a, id_b


def merge_stats(
    keep_alpha: float, keep_beta: float, remove_alpha: float, remove_beta: float
) -> tuple[float, float, float]:
    """α/βの合算（事前分布1,1の二重計上を除く）と再計算した信頼度を返す。
    既存 /intelligence/merge-duplicates と同一の数式（単一ソース化のためここに集約）。"""
    alpha = keep_alpha + remove_alpha - 1.0
    beta = keep_beta + remove_beta - 1.0
    return alpha, beta, alpha / (alpha + beta)


async def _merge_duplicate_chunks(
    db: AsyncSession, threshold: float, max_pairs: int, dry_run: bool
) -> int:
    """夜間の重複統合: 同一project内の類似度>=threshold のペアを統合（残す側へ実績を合算、
    片方を非推奨化）。auto-ingest が同時多発で作る近接重複の掃除。embedding 無しや
    pgvector 不在の環境では安全に0件で返す（degrade-safe）。"""
    if max_pairs <= 0:
        return 0
    q = text("""
        SELECT id_a, id_b, conf_a, conf_b
        FROM (
            SELECT a.id AS id_a, b.id AS id_b,
                   a.confidence_score AS conf_a, b.confidence_score AS conf_b,
                   1 - (a.embedding <=> b.embedding) AS similarity
            FROM kb_chunks a
            JOIN kb_chunks b ON a.id < b.id
                 AND a.project_key = b.project_key
            WHERE a.embedding IS NOT NULL
                  AND b.embedding IS NOT NULL
                  AND a.is_deprecated = false
                  AND b.is_deprecated = false
        ) sub
        WHERE similarity >= :threshold
        ORDER BY similarity DESC
        LIMIT :lim
    """)
    # この検出SELECTは project 内で O(n²)（全ペア×pgvector距離）になりうる。データ増（6月で
    # +2万件）に伴い、advisory lock を掴んだまま無限実行→夜間ラン全体を止める事故が起きた
    # （2026-06-30）。検出SELECTのみ savepoint + statement_timeout で上限を設け、超過時は
    # degrade-safe に 0 件で抜ける（decay/採点/digest は通常どおり継続し、run_date は前進する）。
    # 根本（全ペア結合→近傍ANN検索への置換）は別途 cto-room-dev で対応。
    try:
        async with db.begin_nested():
            await db.execute(text("SET LOCAL statement_timeout = '45s'"))
            rows = (await db.execute(q, {"threshold": threshold, "lim": max_pairs})).all()
    except Exception:
        return 0

    merged = 0
    deprecated: set[int] = set()
    for row in rows:
        # 連鎖ペア(a-b, b-c)対策: このパスで既に消した側が絡むペアはスキップ
        if row.id_a in deprecated or row.id_b in deprecated:
            continue
        keep_id, remove_id = pick_keeper(row.id_a, row.conf_a, row.id_b, row.conf_b)
        if dry_run:
            deprecated.add(remove_id)
            merged += 1
            continue
        keep = await db.get(KbChunk, keep_id)
        remove = await db.get(KbChunk, remove_id)
        if not keep or not remove or keep.is_deprecated or remove.is_deprecated:
            continue
        keep.helpful_count += remove.helpful_count
        keep.unhelpful_count += remove.unhelpful_count
        keep.recall_count += remove.recall_count
        keep.alpha, keep.beta, keep.confidence_score = merge_stats(
            keep.alpha, keep.beta, remove.alpha, remove.beta
        )
        keep.tags = list(set((keep.tags or []) + (remove.tags or [])))
        remove.is_deprecated = True
        deprecated.add(remove_id)
        merged += 1
    return merged


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


async def _knowledge_gaps(db: AsyncSession, day_start: datetime) -> tuple[int, list[str]]:
    """メタ認知(planning): その日の recall で result_count=0 だったクエリ＝知識が無かった所。
    「次に何を学ぶ/取り込むべきか」の一次情報。戻り値: (0件recall総数, 頻出クエリ上位)。"""
    _, day_end = _day_bounds(day_start.date())
    params = {"ds": day_start, "de": day_end}
    total = await db.execute(
        text(
            "SELECT count(*) FROM kb_recall_log "
            "WHERE created_at >= :ds AND created_at < :de AND result_count = 0"
        ),
        params,
    )
    gap_count = int(total.scalar() or 0)
    samples_rows = await db.execute(
        text(
            "SELECT query, count(*) AS c FROM kb_recall_log "
            "WHERE created_at >= :ds AND created_at < :de AND result_count = 0 "
            "GROUP BY query ORDER BY c DESC LIMIT 5"
        ),
        params,
    )
    samples = [row.query for row in samples_rows]
    return gap_count, samples


# ---------------------------------------------------------------------------
# Phase S1: 採点純粋関数（DB非依存・テスト可能）
# ---------------------------------------------------------------------------

def attribute_outcome(
    recall_created: datetime,
    sessions: list[tuple[datetime, str]],
    window_hours: int,
) -> str | None:
    """recall ログの作成時刻から window_hours 以内で最初に完了したセッションの outcome を返す。
    sessions は (created_at, status) のリストで時刻昇順を想定。
    該当なし・partial はどちらも None を返す（α/β の変更なし）。"""
    window_end = recall_created + timedelta(hours=window_hours)
    for created_at, status in sessions:
        if created_at < recall_created:
            continue
        if created_at >= window_end:
            break
        if status == "success":
            return "success"
        if status == "fail":
            return "fail"
        # partial / その他 → スキップして次を見る
    return None


def clamp_delta(current_total: float, step: float, cap: float) -> float:
    """current_total は今晩のこのチャンクへの累計変動量。
    cap を超えない範囲で step を加算した後の累計を返す。
    戻り値は「今回の実際の加算量」（0 以上）。"""
    remaining = max(0.0, cap - current_total)
    return min(step, remaining)


async def _score_recalls(
    db: AsyncSession,
    req: "NightlyRunRequest",
) -> tuple[int, int, int, int]:
    """採点ステップ本体。
    戻り値: (processed, matched, alpha_applied, beta_applied)
    req.score_dry_run=True のときは scored_at を打刻しない（DB変更なし）。
    """
    cutoff = datetime.now(UTC) - timedelta(hours=req.score_window_hours)

    # 未採点ログを古い順に score_batch 件取得
    rows = (await db.execute(
        text(
            "SELECT id, project_key, returned_chunk_ids, created_at "
            "FROM kb_recall_log "
            "WHERE scored_at IS NULL AND created_at < :cutoff "
            "ORDER BY created_at ASC "
            "LIMIT :lim"
        ),
        {"cutoff": cutoff, "lim": req.score_batch},
    )).all()

    processed = len(rows)
    matched = 0
    alpha_applied = 0
    beta_applied = 0

    # チャンク毎の晩内累計変動量（cap 管理用）
    chunk_delta: dict[int, float] = {}
    # 打刻対象 log id
    scored_ids: list[int] = []

    for row in rows:
        log_id: int = row.id
        project_key: str = row.project_key
        chunk_ids: list[int] = row.returned_chunk_ids or []
        recall_ts: datetime = row.created_at

        # タイムゾーン補完（DB値がnaiveの場合）
        if recall_ts.tzinfo is None:
            recall_ts = recall_ts.replace(tzinfo=UTC)

        window_end = recall_ts + timedelta(hours=req.score_window_hours)

        # 同一 project_key のセッションを時刻昇順で取得（window 内）
        sess_rows = (await db.execute(
            text(
                "SELECT created_at, status FROM kb_sessions "
                "WHERE project_key = :pk "
                "  AND created_at >= :ts_start "
                "  AND created_at < :ts_end "
                "ORDER BY created_at ASC "
                "LIMIT 50"
            ),
            {"pk": project_key, "ts_start": recall_ts, "ts_end": window_end},
        )).all()

        sessions_list: list[tuple[datetime, str]] = [
            (r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=UTC), r.status)
            for r in sess_rows
        ]

        outcome = attribute_outcome(recall_ts, sessions_list, req.score_window_hours)

        if outcome is None:
            # partial または該当なし → scored_at は打刻しない（再試行対象のまま）
            continue

        matched += 1
        scored_ids.append(log_id)

        # 先頭 top_n チャンクのみ採点
        target_chunk_ids = chunk_ids[: req.score_top_n]

        for chunk_id in target_chunk_ids:
            # is_deprecated チェック
            dep_row = (await db.execute(
                text("SELECT alpha, beta, is_deprecated FROM kb_chunks WHERE id = :cid"),
                {"cid": chunk_id},
            )).first()
            if not dep_row or dep_row.is_deprecated:
                continue

            cur_alpha: float = dep_row.alpha
            cur_beta: float = dep_row.beta

            if outcome == "success":
                delta = clamp_delta(chunk_delta.get(chunk_id, 0.0), req.score_alpha_step, req.score_daily_cap)
                if delta <= 0:
                    continue
                chunk_delta[chunk_id] = chunk_delta.get(chunk_id, 0.0) + delta
                new_alpha = min(cur_alpha + delta, 100.0)
                new_beta = cur_beta
                alpha_applied += 1
            else:  # fail
                delta = clamp_delta(chunk_delta.get(chunk_id, 0.0), req.score_beta_step, req.score_daily_cap)
                if delta <= 0:
                    continue
                chunk_delta[chunk_id] = chunk_delta.get(chunk_id, 0.0) + delta
                new_alpha = cur_alpha
                new_beta = min(cur_beta + delta, 100.0)
                beta_applied += 1

            if not req.score_dry_run:
                new_conf = new_alpha / (new_alpha + new_beta)
                await db.execute(
                    text(
                        "UPDATE kb_chunks SET alpha=:a, beta=:b, confidence_score=:c "
                        "WHERE id=:cid AND is_deprecated=false"
                    ),
                    {"a": new_alpha, "b": new_beta, "c": new_conf, "cid": chunk_id},
                )

    # scored_at 打刻（score_dry_run=False のときのみ）
    if not req.score_dry_run and scored_ids:
        await db.execute(
            text(
                "UPDATE kb_recall_log SET scored_at = now() "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": scored_ids},
        )

    return processed, matched, alpha_applied, beta_applied


async def _auto_learn_metrics(db: AsyncSession, day_start: datetime) -> tuple[int, int]:
    """auto-ingest 品質メトリクス: その日の kb_sessions のうち tags に 'auto-ingest' を含むものを集計。
    戻り値: (auto_learn_stored, auto_learn_skipped)
    skipped は ingest_state='skipped' のもの（列がなければ0）。"""
    _, day_end = _day_bounds(day_start.date())
    params = {"ds": day_start, "de": day_end}

    stored_row = await db.execute(
        text(
            "SELECT count(*) FROM kb_sessions "
            "WHERE created_at >= :ds AND created_at < :de "
            "  AND tags @> ARRAY['auto-ingest']::text[] "
            "  AND coalesce(ingest_state, '') <> 'skipped'"
        ),
        params,
    )
    stored = int(stored_row.scalar() or 0)

    try:
        skipped_row = await db.execute(
            text(
                "SELECT count(*) FROM kb_sessions "
                "WHERE created_at >= :ds AND created_at < :de "
                "  AND tags @> ARRAY['auto-ingest']::text[] "
                "  AND ingest_state = 'skipped'"
            ),
            params,
        )
        skipped = int(skipped_row.scalar() or 0)
    except Exception:
        skipped = 0

    return stored, skipped


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
        today = datetime.now(UTC).date()
        if req.run_date is not None:
            targets = [req.run_date]
        else:
            targets = await _dates_to_process(db, today, req.catchup_days)

        if not targets:
            return NightlyRunResponse(
                processed=[], north_star_latest=None, dry_run=req.dry_run,
                message="処理対象なし（本日分は完了済み）",
            )

        # decay は「現在状態」操作＝1日1回だけ。**今日分を処理するときのみ**実行し、過去日の遡り
        # (backfill) では実行しない。冪等性：今日が done になれば以降の catch-up 対象から外れるため、
        # 同日に何度呼んでも decay は二重適用されない（手動再実行・cronリトライ安全）。
        cutoff = datetime.now(UTC) - timedelta(days=req.days_threshold)
        do_decay = today in targets
        decayed_total = (
            await _decay(db, cutoff, req.decay_factor, req.min_confidence, req.dry_run) if do_decay else 0
        )
        # 重複統合も decay と同じ「現在状態」操作＝今日分の処理時のみ・1日1回（backfillでは実行しない）
        merged_total = (
            await _merge_duplicate_chunks(db, req.dedup_threshold, req.dedup_max_pairs, req.dry_run)
            if do_decay
            else 0
        )

        for rd in targets:
            day_start, _ = _day_bounds(rd)
            decayed = decayed_total if rd == today else 0
            merged = merged_total if rd == today else 0

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
            gap_count, gap_samples = await _knowledge_gaps(db, day_start)
            auto_stored, auto_skipped = await _auto_learn_metrics(db, day_start)

            # 採点ステップ: targets が空でない（今日分を処理している）夜のみ実行。
            # advisory lock 配下で1回のみ（ループ外の do_decay フラグを流用）。
            sc_processed = sc_matched = sc_alpha = sc_beta = 0
            if do_decay and rd == today:
                sc_processed, sc_matched, sc_alpha, sc_beta = await _score_recalls(db, req)

            # 提案バジェット（最大3件・ほとんどの日は0が正常）。気づき→報連相の核。
            suggestions: list[str] = []
            if recurrence > 0:
                suggestions.append(
                    f"再発した既知ミス {recurrence}件 → 対策の強化・再発防止を検討（北極星が悪化）"
                )
            if gap_count > 0:
                ex = ("（例: " + " / ".join(gap_samples[:2]) + "）") if gap_samples else ""
                suggestions.append(
                    f"知識ギャップ {gap_count}件（recall 0件）→ 取込/学び追加を検討{ex}"
                )
            suggestions = suggestions[:3]

            note = (
                f"decay {decayed}件 / 重複統合 {merged}件 / 再発(北極星) {recurrence}件 / fail {fail_total}件 / "
                f"採点 {scored}件 / 知識ギャップ {gap_count}件 / 提案 {len(suggestions)}件"
                + (f" / 採点S1 processed={sc_processed} matched={sc_matched} α={sc_alpha} β={sc_beta}"
                   f"{'(dry)' if req.score_dry_run else ''}" if sc_processed > 0 else "")
                + ("（dry-run）" if req.dry_run else "")
            )
            digest_obj = {
                "run_date": rd.isoformat(),
                "decayed_count": decayed,
                "merged_duplicates_count": merged,
                "recurrence_count": recurrence,
                "fail_session_count": fail_total,
                "scored_count": scored,
                "knowledge_gap_count": gap_count,
                "knowledge_gap_samples": gap_samples,
                "suggestions": suggestions,
                "north_star": "recurrence_count（再発した既知ミス件数・低いほど良い）",
                "scoring_processed": sc_processed,
                "scoring_matched": sc_matched,
                "scoring_alpha_applied": sc_alpha,
                "scoring_beta_applied": sc_beta,
                "scoring_dry_run": req.score_dry_run,
                "auto_learn_stored": auto_stored,
                "auto_learn_skipped": auto_skipped,
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
                    knowledge_gap_count=gap_count,
                    merged_duplicates_count=merged,
                    suggestions=suggestions,
                    note=note,
                    scoring_processed=sc_processed,
                    scoring_matched=sc_matched,
                    scoring_alpha_applied=sc_alpha,
                    scoring_beta_applied=sc_beta,
                    scoring_dry_run=req.score_dry_run,
                    auto_learn_stored=auto_stored,
                    auto_learn_skipped=auto_skipped,
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
    gap_count = 0
    merged_count = 0
    suggestions: list[str] = []
    sc_processed = sc_matched = sc_alpha = sc_beta = 0
    sc_dry_run = True
    auto_stored = auto_skipped = 0
    try:
        d = r.digest
        if isinstance(d, str):
            d = json.loads(d)
        d = d or {}
        note = d.get("note", "")
        gap_count = int(d.get("knowledge_gap_count", 0) or 0)
        merged_count = int(d.get("merged_duplicates_count", 0) or 0)
        suggestions = list(d.get("suggestions", []) or [])
        sc_processed = int(d.get("scoring_processed", 0) or 0)
        sc_matched = int(d.get("scoring_matched", 0) or 0)
        sc_alpha = int(d.get("scoring_alpha_applied", 0) or 0)
        sc_beta = int(d.get("scoring_beta_applied", 0) or 0)
        sc_dry_run = bool(d.get("scoring_dry_run", True))
        auto_stored = int(d.get("auto_learn_stored", 0) or 0)
        auto_skipped = int(d.get("auto_learn_skipped", 0) or 0)
    except Exception:
        pass
    return NightlyDigest(
        run_date=r.run_date.isoformat(),
        status=r.status,
        decayed_count=r.decayed_count,
        recurrence_count=r.recurrence_count,
        scored_count=r.scored_count,
        fail_session_count=r.fail_session_count,
        knowledge_gap_count=gap_count,
        merged_duplicates_count=merged_count,
        suggestions=suggestions,
        note=note,
        scoring_processed=sc_processed,
        scoring_matched=sc_matched,
        scoring_alpha_applied=sc_alpha,
        scoring_beta_applied=sc_beta,
        scoring_dry_run=sc_dry_run,
        auto_learn_stored=auto_stored,
        auto_learn_skipped=auto_skipped,
    )
