"""app.koe_digest（日次ダイジェストの純粋ロジック）＋ /koe/digest ルーターの単体テスト。"""

from datetime import date

from fastapi import FastAPI
from starlette.testclient import TestClient

from app import koe_digest
from app.database import get_db
from app.routers import koe as koe_router


def _rec(title, ts, speakers, lines):
    return {"title": title, "recorded_at": ts, "speakers": speakers, "lines": lines}


# --- build_digest_source ---


def test_build_source_includes_headers_and_lines():
    recs = [
        _rec("朝礼", "2026-06-04 08:00", ["高木", "西原"],
             [{"speaker": "高木", "content": "採用を強化する"}, {"speaker": "西原", "content": "了解です"}]),
    ]
    text = koe_digest.build_digest_source("2026-06-04", recs)
    assert "2026-06-04 の録音記録" in text
    assert "朝礼" in text
    assert "参加者: 高木／西原" in text
    assert "高木: 採用を強化する" in text


def test_build_source_truncates_over_limit():
    big = [{"speaker": "A", "content": "x" * 1000} for _ in range(50)]
    recs = [_rec("長い会議", "2026-06-04 09:00", ["A"], big)]
    text = koe_digest.build_digest_source("2026-06-04", recs, max_chars=2000)
    assert "…(以下省略)" in text
    assert len(text) <= 2000 + 50  # ヘッダ＋省略マーカー分の余裕


# --- fallback_digest ---


def test_fallback_empty():
    assert "録音はありません" in koe_digest.fallback_digest("2026-06-04", [])


def test_fallback_lists_recordings_and_speakers():
    recs = [
        _rec("採用面談", "2026-06-04 10:00", ["高木", "松本"], []),
        _rec("経理打合せ", "2026-06-04 14:00", ["高木", "九条"], []),
    ]
    d = koe_digest.fallback_digest("2026-06-04", recs)
    assert "録音 2 件" in d
    assert "高木" in d and "松本" in d and "九条" in d
    assert "採用面談" in d


# --- /koe/digest ルーター（fake DB・LLMは空キーでフォールバック）---


class _Result:
    def __init__(self, items):
        self._items = items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def __iter__(self):
        return iter(self._items)


class _Rec:
    def __init__(self, rec_id, title, recorded_at, speaker_set):
        self.id = rec_id
        self.title = title
        self.recorded_at = recorded_at
        self.speaker_set = speaker_set


class _Utt:
    def __init__(self, seq, speaker, content):
        self.seq = seq
        self.speaker = speaker
        self.content = content
        self.start_ms = 0
        self.end_ms = 0


class _FakeSession:
    def __init__(self, recordings=None, utterances=None):
        self._recordings = recordings or []
        self._utts = utterances or []
        self.added = []
        self.commits = 0

    async def execute(self, stmt):
        s = str(stmt).lower()
        if "kb_utterances" in s:
            return _Result(self._utts)
        if "kb_recordings" in s:
            return _Result(self._recordings)
        return _Result([])

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1


def _client(session):
    app = FastAPI()
    app.include_router(koe_router.router, prefix="/api")

    async def _fake_get_db():
        yield session

    app.dependency_overrides[get_db] = _fake_get_db
    return TestClient(app)


def test_digest_generates_and_saves_fallback():
    rec = _Rec(1, "採用面談", "2026-06-04 10:00:00+00", ["高木", "松本"])
    utts = [_Utt(0, "高木", "採用を強化したい"), _Utt(1, "松本", "媒体を増やします")]
    sess = _FakeSession(recordings=[rec], utterances=utts)
    resp = _client(sess).post("/api/koe/digest", json={"date": "2026-06-04", "save": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2026-06-04"
    assert body["recording_count"] == 1
    # OpenAIキー無し環境では fallback に落ちる
    assert body["source"] in ("llm", "fallback")
    assert body["digest"]
    # 録音があり save=True → kb_chunks に1件 add ＆ commit
    assert body["saved"] is True
    assert len(sess.added) == 1
    assert sess.added[0].chunk_type == "daily_digest"
    assert sess.added[0].project_key == "lore"


def test_digest_no_recordings_not_saved():
    sess = _FakeSession(recordings=[], utterances=[])
    resp = _client(sess).post("/api/koe/digest", json={"date": "2026-06-04"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["recording_count"] == 0
    assert body["saved"] is False
    assert sess.added == []


def test_jst_day_range_utc():
    # JST 2026-06-04 の1日 = UTC 2026-06-03 15:00 〜 2026-06-04 15:00
    start, end = koe_router._jst_day_range_utc(date(2026, 6, 4))
    assert start.isoformat() == "2026-06-03T15:00:00+00:00"
    assert end.isoformat() == "2026-06-04T15:00:00+00:00"
