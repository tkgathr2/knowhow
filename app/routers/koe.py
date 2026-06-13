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

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import koe_logic
from app.database import get_db
from app.models import KbRecording, KbSpeakerAlias, KbUtterance

router = APIRouter(tags=["koe"])

# 「確定済み」とみなす状態（watermark が既取込として扱う＝再送しない）
_CONFIRMED = ("ingested", "empty")


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
    db: AsyncSession, rec: KbRecording, utt_rows: list[dict], status: str, speakers: list[str], unknown: list[str]
) -> KoeIngestResponse:
    # すでに確定済み（ingested/empty）の再送 → 何もしない（冪等）
    if rec.transcript_status in _CONFIRMED:
        count = await _count_utts(db, rec.id)
        return _resp(rec, "already_ingested", count, list(rec.speaker_set or []), [])

    # ここまで来た rec は pending。新payloadが確定情報（ingested/empty）を持つなら昇格する
    if status != "pending":
        await _add_utterances(db, rec.id, utt_rows)
        rec.transcript_status = status
        rec.speaker_set = speakers
        rec.ingested_at = datetime.now(UTC)
        if unknown:
            rec.meta = {**(rec.meta or {}), "unknown_speakers": unknown}
        await db.commit()
        await db.refresh(rec)
        return _resp(rec, "upgraded", len(utt_rows), speakers, unknown)

    # まだ未生成のまま（文字起こしが来ていない）
    return _resp(rec, "still_pending", 0, list(rec.speaker_set or []), [])


@router.post("/koe/ingest", response_model=KoeIngestResponse)
async def koe_ingest(req: KoeIngestRequest, db: AsyncSession = Depends(get_db)) -> KoeIngestResponse:
    aliases = await _load_aliases(db)
    seg_dicts = [s.model_dump() for s in req.segments]
    utt_rows = koe_logic.build_utterances(seg_dicts, aliases)
    status = koe_logic.decide_status(req.has_transcript, len(utt_rows))
    speakers = koe_logic.speaker_set(utt_rows)
    unknown = koe_logic.unknown_speakers(utt_rows, aliases)

    rec = await _fetch(db, req.plaud_id)
    if rec is not None:
        return await _handle_existing(db, rec, utt_rows, status, speakers, unknown)

    # 新規。pending のときは台帳のみ（発話なし・ingested_at なし）→ watermark から外れ翌日再送される
    rec = KbRecording(
        plaud_id=req.plaud_id,
        title=req.title,
        recorded_at=req.recorded_at,
        duration_minutes=req.duration_minutes,
        transcript_status=status,
        speaker_set=speakers,
        meta={**req.meta, "unknown_speakers": unknown} if unknown else req.meta,
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
        return await _handle_existing(db, rec, utt_rows, status, speakers, unknown)

    await db.refresh(rec)
    return _resp(rec, status, len(utt_rows), speakers, unknown)


async def _add_utterances_via_flush(db: AsyncSession, rec: KbRecording, utt_rows: list[dict]) -> None:
    """新規録音の発話を追加。FK を解決するため recording を flush して id を採番してから紐づける。"""
    await db.flush()
    await _add_utterances(db, rec.id, utt_rows)


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
