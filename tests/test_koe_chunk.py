"""app.koe_chunk（チャンク化の純粋ロジック）＋ /koe/process ルーターの単体テスト。"""

from fastapi import FastAPI
from starlette.testclient import TestClient

from app import koe_chunk
from app.database import get_db
from app.routers import koe as koe_router


def _utt(seq, speaker, content, start=0, end=0):
    return {"seq": seq, "speaker": speaker, "start_ms": start, "end_ms": end, "content": content}


# --- chunk_utterances ---


def test_chunk_single_window_when_small():
    utts = [_utt(0, "A", "短い"), _utt(1, "B", "発話")]
    chunks = koe_chunk.chunk_utterances(utts, max_chars=1000)
    assert len(chunks) == 1
    assert chunks[0]["seq_start"] == 0
    assert chunks[0]["seq_end"] == 1
    assert chunks[0]["speakers"] == ["A", "B"]


def test_chunk_splits_on_max_chars():
    utts = [_utt(i, "A", "x" * 100) for i in range(10)]  # 各~102字
    chunks = koe_chunk.chunk_utterances(utts, max_chars=250)  # 2発話で超える
    assert len(chunks) >= 4
    # seq は連続して全件カバー（取りこぼしなし）
    assert chunks[0]["seq_start"] == 0
    assert chunks[-1]["seq_end"] == 9
    covered = sum(c["seq_end"] - c["seq_start"] + 1 for c in chunks)
    assert covered == 10


def test_chunk_oversized_single_utterance_kept():
    utts = [_utt(0, "A", "y" * 5000)]
    chunks = koe_chunk.chunk_utterances(utts, max_chars=1000)
    assert len(chunks) == 1
    assert chunks[0]["seq_start"] == 0


def test_chunk_speakers_dedup_in_order():
    utts = [_utt(0, "高木", "a"), _utt(1, "西原", "b"), _utt(2, "高木", "c")]
    chunks = koe_chunk.chunk_utterances(utts, max_chars=1000)
    assert chunks[0]["speakers"] == ["高木", "西原"]


# --- build_chunk_content ---


def test_build_content_with_header():
    ch = {
        "speakers": ["高木", "西原"],
        "lines": [{"speaker": "高木", "content": "おはよう"}, {"speaker": "西原", "content": "どうも"}],
    }
    text = koe_chunk.build_chunk_content(ch, title="朝礼", recorded_at="2026-06-13")
    assert "2026-06-13" in text
    assert "朝礼" in text
    assert "高木／西原" in text
    assert "高木: おはよう" in text
    assert "西原: どうも" in text


def test_build_content_no_header_fields():
    ch = {"speakers": [], "lines": [{"speaker": "A", "content": "ひとこと"}]}
    text = koe_chunk.build_chunk_content(ch)
    assert text == "A: ひとこと"


# --- parse_tags ---


def test_parse_tags_json_array():
    assert koe_chunk.parse_tags('["採用", "交通誘導"]') == ["採用", "交通誘導"]


def test_parse_tags_code_fence():
    assert koe_chunk.parse_tags('```json\n["資金繰り", "銀行"]\n```') == ["資金繰り", "銀行"]


def test_parse_tags_delimited_fallback():
    assert koe_chunk.parse_tags("採用、交通誘導、外国人材") == ["採用", "交通誘導", "外国人材"]


def test_parse_tags_dedup_and_cap():
    assert koe_chunk.parse_tags('["a","a","b","c","d","e","f","g"]', max_tags=3) == ["a", "b", "c"]


def test_parse_tags_empty():
    assert koe_chunk.parse_tags(None) == []
    assert koe_chunk.parse_tags("") == []


# --- /koe/process ルーター（statement-aware fake DB・LLM/embは空キーでスキップ）---


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
    def __init__(self, rec_id, plaud_id, status="ingested", title=None, recorded_at=None):
        self.id = rec_id
        self.plaud_id = plaud_id
        self.transcript_status = status
        self.title = title
        self.recorded_at = recorded_at


class _Utt:
    def __init__(self, seq, speaker, content):
        self.seq = seq
        self.speaker = speaker
        self.content = content
        self.start_ms = seq * 100
        self.end_ms = seq * 100 + 50


class _FakeSession:
    def __init__(self, target=None, utterances=None, existing_chunk_count=0):
        self._target = target
        self._utts = utterances or []
        self._chunk_count = existing_chunk_count
        self.added = []
        self.commits = 0

    async def execute(self, stmt):
        s = str(stmt).lower()
        if "count" in s and "kb_chunks" in s:
            return _Result([self._chunk_count])
        if "kb_utterances" in s:
            return _Result(self._utts)
        if "kb_recordings" in s:
            return _Result([self._target] if self._target else [])
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


def test_process_creates_chunks():
    rec = _Rec(1, "rec1", title="商談", recorded_at="2026-06-13")
    utts = [_Utt(0, "高木", "x" * 100), _Utt(1, "西原", "y" * 100)]
    sess = _FakeSession(target=rec, utterances=utts, existing_chunk_count=0)
    resp = _client(sess).post("/api/koe/process", json={"plaud_id": "rec1", "max_chars": 250})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["status"] == "processed"
    assert body["results"][0]["chunk_count"] >= 1
    # kb_chunks へ project_key='lore'・source_type='recording' で add される
    assert sess.added
    chunk = sess.added[0]
    assert chunk.project_key == "lore"
    assert chunk.source_type == "recording"
    assert chunk.confidence_score == 0.9  # 検索閾値0.70を超える


def test_process_skips_when_already_chunked():
    rec = _Rec(2, "rec2")
    sess = _FakeSession(target=rec, utterances=[_Utt(0, "A", "a")], existing_chunk_count=3)
    resp = _client(sess).post("/api/koe/process", json={"plaud_id": "rec2"})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "skipped"
    assert sess.added == []


def test_process_no_utterances():
    rec = _Rec(3, "rec3")
    sess = _FakeSession(target=rec, utterances=[], existing_chunk_count=0)
    resp = _client(sess).post("/api/koe/process", json={"plaud_id": "rec3"})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "no_utterances"


def test_process_unknown_plaud_id_returns_empty():
    sess = _FakeSession(target=None)
    resp = _client(sess).post("/api/koe/process", json={"plaud_id": "nope"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
