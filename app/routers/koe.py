"""こえキング（録音資産化）Phase 0 のAPI。

- POST /api/koe/ingest      : Plaud録音1件（メタ＋発話セグメント）を取り込む（write保護）
- GET  /api/koe/recordings  : 取込済みの一覧（ids_only=true で plaud_id 集合＝watermark用）

追加のみ・既存無改変。kb_sessions / 夜間採点には触れない。
チャンク化＋LLM話題タグ＋embedding（kb_chunks 相乗り）は後続PRで実装する。

ライフサイクル（重要）:
  Plaud の文字起こしは後から生成されるため、録音は「未生成(pending)」で先に台帳へ載り、
  生成後に再送されて「確定(ingested/empty)」へ昇格する。watermark（ids_only）は確定済みだけを
  「既取込」として返すので、pending の録音は翌日以降のバッチで再送され、昇格の機会を得る。
"""

from datetime import UTC, datetime, timedelta, timezone
from datetime import date as date_cls

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import koe_chunk, koe_digest, koe_filter, koe_logic, koe_tag
from app.auth import require_api_key
from app.config import settings
from app.database import get_db
from app.embedding import create_embedding
from app.models import KbChunk, KbRecording, KbSignal, KbSpeakerAlias, KbUtterance

_JST = timezone(timedelta(hours=9))

router = APIRouter(tags=["koe"])

# 書き込み系（取込・処理・生成）に付ける X-API-Key 必須ガード。
# 読み取り系（GET digest / recordings）は router では保護せず、ブラウザは Google ログイン
# （middleware）で、バッチは X-API-Key で読む（main.py / middleware.py 参照）。
_WRITE_GUARD = [Depends(require_api_key)]

# 「確定済み」とみなす状態（watermark が既取込として扱う＝再送しない）
# noise＝会話でない（機内アナウンス・PA・環境音）。台帳には残すが検索/ダイジェスト対象外。
_CONFIRMED = ("ingested", "empty", "noise")

# プロダクト名「ロア（Lore）」。録音由来の検索資産はこの project_key で kb_chunks に相乗りする。
LORE_PROJECT = "lore"

# 日次ダイジェストのチャンクは録音本体ではない（要約）ので source_id を持たない番兵値。
_DIGEST_SOURCE_ID = 0


class Segment(BaseModel):
    speaker: str | None = None
    original_speaker: str | None = None
    start_time: int = 0
    end_time: int = 0
    content: str = ""


class KoeIngestRequest(BaseModel):
    plaud_id: str
    title: str | None = None
    recorded_at: datetime | None = None
    duration_minutes: int | None = None
    has_transcript: bool = True
    segments: list[Segment] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)


class KoeIngestResponse(BaseModel):
    recording_id: int
    plaud_id: str
    # ingested | empty | pending | upgraded | still_pending | already_ingested
    status: str
    utterance_count: int
    speakers: list[str]
    unknown_speakers: list[str]


async def _load_aliases(db: AsyncSession) -> dict[str, str]:
    rows = await db.execute(select(KbSpeakerAlias))
    return {a.alias: a.canonical for a in rows.scalars()}


async def _fetch(db: AsyncSession, plaud_id: str) -> KbRecording | None:
    rows = await db.execute(select(KbRecording).where(KbRecording.plaud_id == plaud_id))
    return rows.scalar_one_or_none()


async def _count_utts(db: AsyncSession, recording_id: int) -> int:
    rows = await db.execute(
        select(func.count()).select_from(KbUtterance).where(KbUtterance.recording_id == recording_id)
    )
    return int(rows.scalar() or 0)


def _resp(rec, status, count, speakers, unknown) -> KoeIngestResponse:
    return KoeIngestResponse(
        recording_id=rec.id,
        plaud_id=rec.plaud_id,
        status=status,
        utterance_count=count,
        speakers=speakers,
        unknown_speakers=unknown,
    )


async def _add_utterances(db: AsyncSession, recording_id: int, utt_rows: list[dict]) -> None:
    for u in utt_rows:
        db.add(
            KbUtterance(
                recording_id=recording_id,
                seq=u["seq"],
                speaker=u["speaker"],
                speaker_raw=u["speaker_raw"],
                start_ms=u["start_ms"],
                end_ms=u["end_ms"],
                content=u["content"],
            )
        )


async def _handle_existing(
    db: AsyncSession, rec: KbRecording, utt_rows: list[dict], status: str,
    speakers: list[str], unknown: list[str], filter_meta: dict | None = None,
) -> KoeIngestResponse:
    # すでに確定済み（ingested/empty）の再送 → 何もしない（冪等）
    if rec.transcript_status in ("ingested", "empty"):
        count = await _count_utts(db, rec.id)
        return _resp(rec, "already_ingested", count, list(rec.speaker_set or []), [])

    # noise だった録音：新payloadが会話/会議ソースで ingested 判定なら noise を解除して昇格。
    # 発話は初回ingestで既に保存済みなので再追加しない（status を ingested に上げるだけ）。
    if rec.transcript_status == "noise":
        if status == "ingested":
            rec.transcript_status = "ingested"
            rec.ingested_at = datetime.now(UTC)
            rec.speaker_set = speakers
            new_meta = {**(rec.meta or {}), **(filter_meta or {})}
            if unknown:
                new_meta["unknown_speakers"] = unknown
            rec.meta = new_meta
            await db.commit()
            await db.refresh(rec)
            count = await _count_utts(db, rec.id)
            return _resp(rec, "upgraded", count, speakers, unknown)
        return _resp(rec, "already_ingested", await _count_utts(db, rec.id), list(rec.speaker_set or []), [])

    # ここまで来た rec は pending。新payloadが確定情報（ingested/empty）を持つなら昇格する
    if status != "pending":
        await _add_utterances(db, rec.id, utt_rows)
        rec.transcript_status = status
        rec.speaker_set = speakers
        rec.ingested_at = datetime.now(UTC)
        new_meta = {**(rec.meta or {}), **(filter_meta or {})}
        if unknown:
            new_meta["unknown_speakers"] = unknown
        rec.meta = new_meta
        await db.commit()
        await db.refresh(rec)
        return _resp(rec, "upgraded", len(utt_rows), speakers, unknown)

    # まだ未生成のまま（文字起こしが来ていない）
    return _resp(rec, "still_pending", 0, list(rec.speaker_set or []), [])


@router.post("/koe/ingest", response_model=KoeIngestResponse, dependencies=_WRITE_GUARD)
async def koe_ingest(req: KoeIngestRequest, db: AsyncSession = Depends(get_db)) -> KoeIngestResponse:
    aliases = await _load_aliases(db)
    seg_dicts = [s.model_dump() for s in req.segments]
    utt_rows = koe_logic.build_utterances(seg_dicts, aliases)
    status = koe_logic.decide_status(req.has_transcript, len(utt_rows))
    # 会話フィルタ：機内アナウンス・PA・環境音など「会話でない」録音は noise にして
    # チャンク化・ダイジェストの対象から外す（社長の実会話だけをロアに残す）。
    # ただし会議ソース（tl;dv/Zoom/Meet/Teams）は元々が会議＝必ず会話なのでフィルタを通さない。
    filter_meta: dict = {}
    meeting_source = str(req.meta.get("source", "")).lower() in ("tldv", "zoom", "meet", "teams")
    if status == "ingested" and not meeting_source:
        is_conv, reason, score = koe_filter.is_conversation(utt_rows)
        filter_meta = {"filter": reason, "conv_score": score}
        if not is_conv:
            status = "noise"
    speakers = koe_logic.speaker_set(utt_rows)
    unknown = koe_logic.unknown_speakers(utt_rows, aliases)

    rec = await _fetch(db, req.plaud_id)
    if rec is not None:
        return await _handle_existing(db, rec, utt_rows, status, speakers, unknown, filter_meta)

    base_meta = {**req.meta, **filter_meta}
    if unknown:
        base_meta["unknown_speakers"] = unknown
    # 新規。pending のときは台帳のみ（発話なし・ingested_at なし）→ watermark から外れ翌日再送される
    rec = KbRecording(
        plaud_id=req.plaud_id,
        title=req.title,
        recorded_at=req.recorded_at,
        duration_minutes=req.duration_minutes,
        transcript_status=status,
        speaker_set=speakers,
        meta=base_meta,
        ingested_at=datetime.now(UTC) if status in _CONFIRMED else None,
    )
    db.add(rec)
    await _add_utterances_via_flush(db, rec, utt_rows)

    try:
        await db.commit()
    except IntegrityError:
        # 競合（同一 plaud_id を並行/再送で挿入）→ ロールバックして既存として処理（冪等）
        await db.rollback()
        rec = await _fetch(db, req.plaud_id)
        if rec is None:
            raise
        return await _handle_existing(db, rec, utt_rows, status, speakers, unknown, filter_meta)

    await db.refresh(rec)
    return _resp(rec, status, len(utt_rows), speakers, unknown)


async def _add_utterances_via_flush(db: AsyncSession, rec: KbRecording, utt_rows: list[dict]) -> None:
    """新規録音の発話を追加。FK を解決するため recording を flush して id を採番してから紐づける。"""
    await db.flush()
    await _add_utterances(db, rec.id, utt_rows)


# --- チャンク化＋話題タグ＋embedding（kb_chunks 相乗り）。Plaud同期と独立に動かせる ---


class ProcessRequest(BaseModel):
    plaud_id: str | None = None  # 指定でその1件、未指定で未処理を limit 件
    limit: int = Field(default=20, ge=1, le=200)
    max_chars: int = Field(default=koe_chunk.DEFAULT_MAX_CHARS, ge=200, le=8000)


class ProcessResult(BaseModel):
    plaud_id: str
    status: str  # processed | skipped | no_utterances
    chunk_count: int


class ProcessResponse(BaseModel):
    results: list[ProcessResult]
    total: int


async def _count_chunks(db: AsyncSession, recording_id: int) -> int:
    rows = await db.execute(
        select(func.count())
        .select_from(KbChunk)
        .where(
            KbChunk.project_key == LORE_PROJECT,
            KbChunk.source_type == "recording",
            KbChunk.source_id == recording_id,
        )
    )
    return int(rows.scalar() or 0)


async def _select_process_targets(db: AsyncSession, plaud_id: str | None, limit: int) -> list[KbRecording]:
    if plaud_id:
        rows = await db.execute(select(KbRecording).where(KbRecording.plaud_id == plaud_id))
        rec = rows.scalar_one_or_none()
        return [rec] if rec else []
    # ingested かつ まだ kb_chunks に無い録音だけを対象（empty は本文が無いのでチャンク不要）
    already = select(KbChunk.source_id).where(
        KbChunk.project_key == LORE_PROJECT, KbChunk.source_type == "recording"
    )
    rows = await db.execute(
        select(KbRecording)
        .where(KbRecording.transcript_status == "ingested", KbRecording.id.notin_(already))
        .order_by(KbRecording.recorded_at.desc().nullslast())
        .limit(limit)
    )
    return list(rows.scalars())


async def _fetch_utterances(db: AsyncSession, recording_id: int) -> list[dict]:
    rows = await db.execute(
        select(KbUtterance).where(KbUtterance.recording_id == recording_id).order_by(KbUtterance.seq)
    )
    return [
        {
            "seq": u.seq,
            "speaker": u.speaker,
            "start_ms": u.start_ms,
            "end_ms": u.end_ms,
            "content": u.content,
        }
        for u in rows.scalars()
    ]


@router.post("/koe/process", response_model=ProcessResponse, dependencies=_WRITE_GUARD)
async def koe_process(req: ProcessRequest, db: AsyncSession = Depends(get_db)) -> ProcessResponse:
    """取込済み録音を話題チャンク化→話題タグ→embedding して kb_chunks(project='lore') に保存。

    冪等：既にチャンクがある録音は skip。LLMタグ/embedding はベストエフォート（失敗しても保存）。
    """
    targets = await _select_process_targets(db, req.plaud_id, req.limit)
    results: list[ProcessResult] = []

    for rec in targets:
        if await _count_chunks(db, rec.id) > 0:
            results.append(ProcessResult(plaud_id=rec.plaud_id, status="skipped", chunk_count=0))
            continue

        utterances = await _fetch_utterances(db, rec.id)
        if not utterances:
            results.append(ProcessResult(plaud_id=rec.plaud_id, status="no_utterances", chunk_count=0))
            continue

        chunks = koe_chunk.chunk_utterances(utterances, req.max_chars)
        recorded_at_str = str(rec.recorded_at) if rec.recorded_at else None
        count = 0
        for ch in chunks:
            content = koe_chunk.build_chunk_content(ch, rec.title, rec.recorded_at)
            tags = await koe_tag.tag_chunk(content)
            embedding = None
            try:
                embedding = await create_embedding(content)
            except Exception:
                embedding = None

            chunk = KbChunk(
                project_key=LORE_PROJECT,
                source_type="recording",
                source_id=rec.id,
                chunk_type="recording",
                content=content,
                tags=tags,
                # 録音は「社長が実際に話した事実」＝高信頼。検索閾値(0.70)を超える値を与える（HO-83の罠回避）
                importance_score=6,
                confidence_score=0.9,
                alpha=9.0,
                beta=1.0,
                meta={
                    "plaud_id": rec.plaud_id,
                    "recorded_at": recorded_at_str,
                    "speakers": ch["speakers"],
                    "topic_tags": tags,
                    "start_ms": ch["start_ms"],
                    "end_ms": ch["end_ms"],
                    "seq_start": ch["seq_start"],
                    "seq_end": ch["seq_end"],
                },
            )
            if embedding is not None:
                chunk.embedding = embedding
                chunk.embedding_model = settings.embedding_model
                chunk.embedding_dimensions = settings.embedding_dim
            db.add(chunk)
            count += 1

        await db.commit()
        results.append(ProcessResult(plaud_id=rec.plaud_id, status="processed", chunk_count=count))

    return ProcessResponse(results=results, total=len(results))


# --- 日次ダイジェスト（その日の録音→決めたこと/約束/人物別 を経営ダイジェスト化）---


class DigestRequest(BaseModel):
    date: date_cls  # JST の日付（例: 2026-06-04）
    save: bool = True


class DigestResponse(BaseModel):
    date: str
    recording_count: int
    digest: str
    source: str  # llm | fallback
    saved: bool


def _jst_day_range_utc(d: date_cls) -> tuple[datetime, datetime]:
    """JST の1日 [d 00:00, d+1 00:00) を UTC の半開区間に変換する。"""
    start_jst = datetime(d.year, d.month, d.day, tzinfo=_JST)
    end_jst = start_jst + timedelta(days=1)
    return start_jst.astimezone(UTC), end_jst.astimezone(UTC)


async def _recordings_on(db: AsyncSession, d: date_cls) -> list[KbRecording]:
    start_utc, end_utc = _jst_day_range_utc(d)
    rows = await db.execute(
        select(KbRecording)
        .where(
            KbRecording.transcript_status == "ingested",
            KbRecording.recorded_at >= start_utc,
            KbRecording.recorded_at < end_utc,
        )
        .order_by(KbRecording.recorded_at.asc())
    )
    return list(rows.scalars())


@router.post("/koe/digest", response_model=DigestResponse, dependencies=_WRITE_GUARD)
async def koe_digest_generate(req: DigestRequest, db: AsyncSession = Depends(get_db)) -> DigestResponse:
    """指定日(JST)の録音をまとめて経営ダイジェストを生成（save=True で kb_chunks に保存）。"""
    date_label = req.date.isoformat()
    recordings = await _recordings_on(db, req.date)

    rec_dicts: list[dict] = []
    for rec in recordings:
        lines = [{"speaker": u["speaker"], "content": u["content"]} for u in await _fetch_utterances(db, rec.id)]
        rec_dicts.append(
            {
                "title": rec.title,
                "recorded_at": str(rec.recorded_at) if rec.recorded_at else None,
                "speakers": list(rec.speaker_set or []),
                "lines": lines,
            }
        )

    source_text = koe_digest.build_digest_source(date_label, rec_dicts)
    digest = await koe_tag.generate_daily_digest(source_text)
    source = "llm"
    if not digest:
        digest = koe_digest.fallback_digest(date_label, rec_dicts)
        source = "fallback"

    saved = False
    if req.save and recordings:
        db.add(
            KbChunk(
                project_key=LORE_PROJECT,
                source_type="digest",
                source_id=_DIGEST_SOURCE_ID,
                chunk_type="daily_digest",
                content=digest,
                tags=["日次ダイジェスト", date_label],
                importance_score=7,
                # ダイジェストは「日付で取り出す/朝届ける」もので検索資産ではない。
                # confidence を検索閾値(0.70)未満にして /search・/recall の網から外す（要約が生録音を押しのけない）。
                confidence_score=0.5,
                meta={"date": date_label, "recording_count": len(recordings), "source": source},
            )
        )
        await db.commit()
        saved = True

    return DigestResponse(
        date=date_label,
        recording_count=len(recordings),
        digest=digest,
        source=source,
        saved=saved,
    )


@router.get("/koe/digest", response_model=DigestResponse)
async def koe_digest_get(
    date: date_cls = Query(...), db: AsyncSession = Depends(get_db)
) -> DigestResponse:
    """保存済みの日次ダイジェストを取得（同日複数あれば最新）。"""
    date_label = date.isoformat()
    rows = await db.execute(
        select(KbChunk)
        .where(
            KbChunk.project_key == LORE_PROJECT,
            KbChunk.chunk_type == "daily_digest",
            KbChunk.meta["date"].astext == date_label,
        )
        .order_by(KbChunk.created_at.desc())
        .limit(1)
    )
    chunk = rows.scalar_one_or_none()
    if chunk is None:
        return DigestResponse(date=date_label, recording_count=0, digest="", source="none", saved=False)
    meta = chunk.meta or {}
    return DigestResponse(
        date=date_label,
        recording_count=int(meta.get("recording_count", 0)),
        digest=chunk.content,
        source=str(meta.get("source", "llm")),
        saved=True,
    )


class DigestListItem(BaseModel):
    date: str
    digest: str
    recording_count: int
    source: str


class DigestListResponse(BaseModel):
    digests: list[DigestListItem]
    total: int


@router.get("/koe/digests", response_model=DigestListResponse)
async def koe_digests(
    days: int = Query(default=14, ge=1, le=90), db: AsyncSession = Depends(get_db)
) -> DigestListResponse:
    """保存済みの日次ダイジェストを新しい日付順でまとめて返す（数日分を一覧表示する用）。

    同じ日付に複数あれば最新(created_at)の1件のみ。最大 days 件。
    """
    rows = await db.execute(
        select(KbChunk)
        .where(KbChunk.project_key == LORE_PROJECT, KbChunk.chunk_type == "daily_digest")
        .order_by(KbChunk.created_at.desc())
    )
    seen: set[str] = set()
    items: list[DigestListItem] = []
    for c in rows.scalars():
        meta = c.meta or {}
        d = meta.get("date")
        if not d or d in seen:
            continue
        seen.add(d)
        items.append(
            DigestListItem(
                date=d,
                digest=c.content,
                recording_count=int(meta.get("recording_count", 0)),
                source=str(meta.get("source", "llm")),
            )
        )
    items.sort(key=lambda x: x.date, reverse=True)
    items = items[:days]
    return DigestListResponse(digests=items, total=len(items))


class RecordingItem(BaseModel):
    plaud_id: str
    title: str | None
    recorded_at: datetime | None
    duration_minutes: int | None
    transcript_status: str
    speakers: list[str]


class RecordingsResponse(BaseModel):
    recordings: list[RecordingItem] | None = None
    plaud_ids: list[str] | None = None
    total: int


@router.get("/koe/recordings", response_model=RecordingsResponse)
async def koe_recordings(
    ids_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> RecordingsResponse:
    if ids_only:
        # watermark 用：確定済み（ingested/empty）の plaud_id を全件返す。
        # pending は「未取込」として意図的に除外＝翌日のバッチで再送され昇格の機会を得る。
        # limit はここでは意図的に適用しない（取込済み集合は全件必要）。
        rows = await db.execute(
            select(KbRecording.plaud_id).where(KbRecording.transcript_status.in_(_CONFIRMED))
        )
        ids = [r[0] for r in rows.all()]
        return RecordingsResponse(plaud_ids=ids, total=len(ids))

    rows = await db.execute(
        select(KbRecording).order_by(KbRecording.recorded_at.desc().nullslast()).limit(limit)
    )
    items = [
        RecordingItem(
            plaud_id=r.plaud_id,
            title=r.title,
            recorded_at=r.recorded_at,
            duration_minutes=r.duration_minutes,
            transcript_status=r.transcript_status,
            speakers=list(r.speaker_set or []),
        )
        for r in rows.scalars()
    ]
    return RecordingsResponse(recordings=items, total=len(items))


# --- 経営判断シグナル（秋好モデル③：録音→"効くものだけ"自動抽出）---


class SignalGenRequest(BaseModel):
    date: date_cls  # JST の日付
    save: bool = True


class SignalItem(BaseModel):
    id: int | None = None
    signal_date: str
    signal_type: str
    title: str
    detail: str | None = None
    who: str | None = None
    importance: int
    status: str = "open"
    source_recording_id: int | None = None


class SignalGenResponse(BaseModel):
    date: str
    recording_count: int
    extracted: int  # LLM が出した件数
    saved: int  # 新規保存できた件数（重複は除く）
    source: str  # llm | empty
    signals: list[SignalItem]


class SignalListResponse(BaseModel):
    signals: list[SignalItem]
    total: int


class SignalPatchRequest(BaseModel):
    status: str  # open | done | dismissed


_SIGNAL_STATUSES = {"open", "done", "dismissed"}


@router.post("/koe/signals", response_model=SignalGenResponse, dependencies=_WRITE_GUARD)
async def koe_signals_generate(req: SignalGenRequest, db: AsyncSession = Depends(get_db)) -> SignalGenResponse:
    """指定日(JST)の録音から経営判断シグナルを抽出（save=True で kb_signals に冪等保存）。

    ダイジェストと同じ入力テキストを使い、LLM が『社長が知る/判断すべきこと』だけを構造化。
    同一日の再実行は dedup_hash で重複を弾く（冪等）。
    """
    date_label = req.date.isoformat()
    recordings = await _recordings_on(db, req.date)

    rec_dicts: list[dict] = []
    for rec in recordings:
        lines = [{"speaker": u["speaker"], "content": u["content"]} for u in await _fetch_utterances(db, rec.id)]
        rec_dicts.append(
            {
                "title": rec.title,
                "recorded_at": str(rec.recorded_at) if rec.recorded_at else None,
                "speakers": list(rec.speaker_set or []),
                "lines": lines,
            }
        )

    source_text = koe_digest.build_digest_source(date_label, rec_dicts)
    extracted = await koe_tag.extract_signals(source_text)
    source = "llm" if extracted else "empty"

    saved = 0
    out: list[SignalItem] = []
    if req.save:
        for s in extracted:
            row = KbSignal(
                project_key=LORE_PROJECT,
                signal_date=req.date,
                signal_type=s["signal_type"],
                title=s["title"],
                detail=s.get("detail"),
                who=s.get("who"),
                importance=s["importance"],
                status="open",
                dedup_hash=s["dedup_hash"],
                meta={"date": date_label, "recording_count": len(recordings), "source": source},
            )
            # SAVEPOINT 単位で 1 行ずつ挿入。dedup ユニーク制約に当たった行だけを
            # ロールバックし、既に保存済みの行は残す（セッション全体は巻き戻さない）。
            try:
                async with db.begin_nested():
                    db.add(row)
                    await db.flush()
            except IntegrityError:
                continue
            saved += 1
            out.append(
                SignalItem(
                    id=row.id,
                    signal_date=date_label,
                    signal_type=row.signal_type,
                    title=row.title,
                    detail=row.detail,
                    who=row.who,
                    importance=row.importance,
                    status=row.status,
                    source_recording_id=row.source_recording_id,
                )
            )
        await db.commit()
    else:
        out = [
            SignalItem(
                signal_date=date_label,
                signal_type=s["signal_type"],
                title=s["title"],
                detail=s.get("detail"),
                who=s.get("who"),
                importance=s["importance"],
            )
            for s in extracted
        ]

    return SignalGenResponse(
        date=date_label,
        recording_count=len(recordings),
        extracted=len(extracted),
        saved=saved,
        source=source,
        signals=out,
    )


@router.get("/koe/signals", response_model=SignalListResponse)
async def koe_signals_list(
    date: date_cls | None = Query(default=None),
    signal_type: str | None = Query(default=None),
    status: str | None = Query(default="open"),
    min_importance: int = Query(default=1, ge=1, le=10),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> SignalListResponse:
    """溜まった経営判断シグナルを取り出す。既定は status=open を重要度→新しい順で。"""
    q = select(KbSignal).where(
        KbSignal.project_key == LORE_PROJECT,
        KbSignal.importance >= min_importance,
    )
    if date is not None:
        q = q.where(KbSignal.signal_date == date)
    if signal_type:
        q = q.where(KbSignal.signal_type == signal_type)
    if status and status != "all":
        q = q.where(KbSignal.status == status)
    q = q.order_by(KbSignal.importance.desc(), KbSignal.signal_date.desc(), KbSignal.id.desc()).limit(limit)

    rows = await db.execute(q)
    items = [
        SignalItem(
            id=r.id,
            signal_date=r.signal_date.isoformat(),
            signal_type=r.signal_type,
            title=r.title,
            detail=r.detail,
            who=r.who,
            importance=r.importance,
            status=r.status,
            source_recording_id=r.source_recording_id,
        )
        for r in rows.scalars()
    ]
    return SignalListResponse(signals=items, total=len(items))


@router.patch("/koe/signals/{signal_id}", response_model=SignalItem, dependencies=_WRITE_GUARD)
async def koe_signal_update(
    signal_id: int, req: SignalPatchRequest, db: AsyncSession = Depends(get_db)
) -> SignalItem:
    """シグナルの対応状態を更新（open→done/dismissed）。"""
    if req.status not in _SIGNAL_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(_SIGNAL_STATUSES)}")
    row = await db.get(KbSignal, signal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="signal not found")
    row.status = req.status
    await db.commit()
    return SignalItem(
        id=row.id,
        signal_date=row.signal_date.isoformat(),
        signal_type=row.signal_type,
        title=row.title,
        detail=row.detail,
        who=row.who,
        importance=row.importance,
        status=row.status,
        source_recording_id=row.source_recording_id,
    )
