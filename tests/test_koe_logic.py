"""app.koe_logic（純粋ロジック）＋ koe ルーターの単体テスト。

ルーターテストの DB は statement-aware な fake セッションで差し替える
（呼び出し順ではなく、実行される SQL の対象テーブルで結果を出し分けるため、
ルーターのクエリ追加・並べ替えに対して脆くない）。
"""

from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from starlette.testclient import TestClient

from app import koe_logic
from app.auth import require_api_key
from app.database import get_db
from app.routers import koe as koe_router

ALIASES = {"Atsuhiro Takagi": "高木豊大", "髙木豊大": "高木豊大", "西原さん": "西原"}


# --- 純粋ロジック ---


def test_normalize_speaker_alias_and_unknown():
    assert koe_logic.normalize_speaker("Atsuhiro Takagi", ALIASES) == "高木豊大"
    assert koe_logic.normalize_speaker("髙木豊大", ALIASES) == "高木豊大"
    assert koe_logic.normalize_speaker("新顔さん", ALIASES) == "新顔さん"
    assert koe_logic.normalize_speaker(None, ALIASES) == "unknown"
    assert koe_logic.normalize_speaker("   ", ALIASES) == "unknown"


def test_build_utterances_skips_empty_and_numbers_seq():
    segs = [
        {"start_time": 0, "end_time": 100, "content": "おはよう", "speaker": "Atsuhiro Takagi"},
        {"start_time": 100, "end_time": 200, "content": "   ", "speaker": "西原さん"},  # 空→skip
        {"start_time": 200, "end_time": 300, "content": "了解", "original_speaker": "Speaker 4"},
    ]
    rows = koe_logic.build_utterances(segs, ALIASES)
    assert len(rows) == 2
    assert [r["seq"] for r in rows] == [0, 1]
    assert rows[0]["speaker"] == "高木豊大"
    assert rows[0]["speaker_raw"] == "Atsuhiro Takagi"
    assert rows[1]["speaker"] == "Speaker 4"
    assert rows[1]["start_ms"] == 200


def test_speaker_set_dedup_in_order():
    utts = [{"speaker": "高木豊大"}, {"speaker": "西原"}, {"speaker": "高木豊大"}]
    assert koe_logic.speaker_set(utts) == ["高木豊大", "西原"]


def test_decide_status():
    assert koe_logic.decide_status(False, 0) == "pending"
    assert koe_logic.decide_status(False, 5) == "pending"
    assert koe_logic.decide_status(True, 0) == "empty"
    assert koe_logic.decide_status(True, 3) == "ingested"


def test_unknown_speakers():
    utts = [
        {"speaker_raw": "Atsuhiro Takagi"},
        {"speaker_raw": "新顔さん"},
        {"speaker_raw": "新顔さん"},
        {"speaker_raw": None},
    ]
    assert koe_logic.unknown_speakers(utts, ALIASES) == ["新顔さん"]


# --- ルーター（statement-aware fake DB）---


class _Result:
    def __init__(self, items):
        self._items = items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalar(self):
        return self._items[0] if self._items else None

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def __iter__(self):
        return iter(self._items)


class _Rec:
    """KbRecording のスタブ。"""

    def __init__(self, rec_id=42, plaud_id="x", status="pending", speaker_set=None, meta=None):
        self.id = rec_id
        self.plaud_id = plaud_id
        self.transcript_status = status
        self.speaker_set = speaker_set or []
        self.meta = meta or {}
        self.ingested_at = None
        self.title = None
        self.recorded_at = None
        self.duration_minutes = None


class _FakeSession:
    """実行される SQL 文の対象テーブルで結果を出し分ける fake AsyncSession。"""

    def __init__(self, existing=None, aliases=None, utt_count=0, commit_raises=False):
        self._existing = existing
        self._aliases = aliases or []
        self._utt_count = utt_count
        self._commit_raises = commit_raises
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt):
        s = str(stmt).lower()
        if "kb_speaker_aliases" in s:
            return _Result(self._aliases)
        if "count" in s and "kb_utterances" in s:
            return _Result([self._utt_count])
        if "kb_recordings" in s:
            return _Result([self._existing] if self._existing else [])
        return _Result([])

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for r in self.added:
            if getattr(r, "id", None) is None and hasattr(r, "plaud_id"):
                r.id = 42

    async def commit(self):
        if self._commit_raises:
            self._commit_raises = False  # 2回目以降は成功（rollback後の経路を試すため）
            raise IntegrityError("dup", {}, Exception("unique"))
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, row):
        if getattr(row, "id", None) is None:
            row.id = 42


def _client(session: _FakeSession) -> TestClient:
    app = FastAPI()
    app.include_router(koe_router.router, prefix="/api")

    async def _fake_get_db():
        yield session

    app.dependency_overrides[get_db] = _fake_get_db
    app.dependency_overrides[require_api_key] = lambda: None  # write系の鍵ガードをテストでは無効化
    return TestClient(app)


def test_ingest_new_recording():
    sess = _FakeSession()
    resp = _client(sess).post(
        "/api/koe/ingest",
        json={
            "plaud_id": "abc123",
            "title": "テスト録音",
            "has_transcript": True,
            "segments": [
                {"start_time": 0, "end_time": 100, "content": "この件どう進める？", "speaker": "Atsuhiro Takagi"},
                {"start_time": 100, "end_time": 200, "content": "媒体を増やしましょう", "speaker": "西原さん"},
                {"start_time": 200, "end_time": 300, "content": "了解、それで進めて", "speaker": "Atsuhiro Takagi"},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ingested"
    assert body["utterance_count"] == 3
    assert "高木豊大" in body["speakers"] or "Atsuhiro Takagi" in body["speakers"]
    assert sess.committed is True
    # recording 1 + utterance 3
    assert len(sess.added) == 4


def test_ingest_new_pending_when_no_transcript():
    sess = _FakeSession()
    resp = _client(sess).post(
        "/api/koe/ingest",
        json={"plaud_id": "p1", "has_transcript": False, "segments": []},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert resp.json()["utterance_count"] == 0
    # 台帳行のみ（発話なし）
    assert len(sess.added) == 1


def test_ingest_noise_recording_marked_noise():
    """機内アナウンス主体の録音は会話フィルタで status=noise になる（チャンク化・ダイジェスト対象外）。"""
    sess = _FakeSession()
    resp = _client(sess).post(
        "/api/koe/ingest",
        json={
            "plaud_id": "flight1",
            "has_transcript": True,
            "segments": [
                {"start_time": 0, "end_time": 100, "speaker": "CA",
                 "content": "シートベルトを腰の低い位置でお締めください。"},
                {"start_time": 100, "end_time": 200, "speaker": "CA2",
                 "content": "Please fasten your seat belt. emergency exits..."},
                {"start_time": 200, "end_time": 300, "speaker": "CA3",
                 "content": "客室乗務員にお知らせください。非常口をご確認ください。"},
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "noise"


def test_ingest_empty_transcript():
    sess = _FakeSession()
    resp = _client(sess).post(
        "/api/koe/ingest",
        json={"plaud_id": "silent1", "has_transcript": True, "segments": []},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "empty"


def test_ingest_idempotent_when_confirmed():
    existing = _Rec(rec_id=7, plaud_id="dup999", status="ingested", speaker_set=["高木豊大"])
    sess = _FakeSession(existing=existing, utt_count=5)
    resp = _client(sess).post("/api/koe/ingest", json={"plaud_id": "dup999", "segments": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "already_ingested"
    assert body["utterance_count"] == 5
    # 確定済みなら何も add しない
    assert sess.added == []


def test_ingest_upgrades_pending_to_ingested():
    """未生成で台帳に載っていた録音に、後から文字起こしが来たら昇格する（MEDIUM-1）。"""
    rec = _Rec(rec_id=9, plaud_id="late1", status="pending")
    sess = _FakeSession(existing=rec)
    resp = _client(sess).post(
        "/api/koe/ingest",
        json={
            "plaud_id": "late1",
            "has_transcript": True,
            "segments": [
                {"start_time": 0, "end_time": 50, "content": "やっと文字起こし来たね", "speaker": "Atsuhiro Takagi"},
                {"start_time": 50, "end_time": 100, "content": "はい、確認します", "speaker": "西原さん"},
                {"start_time": 100, "end_time": 150, "content": "じゃあ進めよう", "speaker": "Atsuhiro Takagi"},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "upgraded"
    assert body["utterance_count"] == 3
    assert rec.transcript_status == "ingested"
    assert rec.ingested_at is not None
    # 昇格時に発話が add される
    assert len(sess.added) == 3


def test_ingest_still_pending_when_transcript_absent():
    rec = _Rec(rec_id=10, plaud_id="late2", status="pending")
    sess = _FakeSession(existing=rec)
    resp = _client(sess).post(
        "/api/koe/ingest",
        json={"plaud_id": "late2", "has_transcript": False, "segments": []},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "still_pending"
    assert rec.transcript_status == "pending"
    assert sess.added == []


def test_ingest_conflict_falls_back_to_existing():
    """並行/再送で UNIQUE 競合 → rollback して既存として冪等に扱う（HIGH-1）。

    競合の再現：最初の重複チェックでは存在せず（None）、INSERT の commit で UNIQUE 違反。
    rollback 後の再 SELECT で（先に入った）既存行が見える。
    """
    winner = _Rec(rec_id=11, plaud_id="race1", status="ingested", speaker_set=["西原"])

    class _ConflictSession(_FakeSession):
        async def execute(self, stmt):
            s = str(stmt).lower()
            if "kb_speaker_aliases" in s:
                return _Result([])
            if "count" in s and "kb_utterances" in s:
                return _Result([3])
            if "kb_recordings" in s:
                # rollback 前は存在せず、rollback 後（競合相手が確定済み）は見える
                return _Result([winner] if self.rolled_back else [])
            return _Result([])

    sess = _ConflictSession(commit_raises=True)
    resp = _client(sess).post(
        "/api/koe/ingest",
        json={
            "plaud_id": "race1",
            "has_transcript": True,
            "segments": [{"start_time": 0, "end_time": 50, "content": "競合", "speaker": "西原さん"}],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_ingested"
    assert sess.rolled_back is True


def test_recordings_ids_only():
    class _S(_FakeSession):
        async def execute(self, stmt):
            # watermark：確定済みの plaud_id を行タプルで返す
            return _Result([("a",), ("b",)])

    resp = _client(_S()).get("/api/koe/recordings", params={"ids_only": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["plaud_ids"] == ["a", "b"]
    assert body["total"] == 2
