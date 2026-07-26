"""バッチ embedding が Gemini 無料枠を浪費しないことを検証する（2026-07-26）。

本番事故の要約:
  batchEmbedContents のペイロード生成に入力長の切り詰めが無く、100 件のうち
  1 件でも 2,048 トークンを超えるとバッチ全体が 400 で落ちていた。その結果
  100 件すべてが単発フォールバックへ回り、1 リクエストで済む処理が 100
  リクエストに膨らんで無料枠の日次上限(1,000)を溶かしていた。
  実測: embedding バックフィルが 1 日 48 件しか進まない（残 22,927 件）。

ここで守る契約:
  1. 長文が混ざってもバッチは 1 リクエストで完了する（事前切り詰め）
  2. それでも 400 が返ったらバッチを半分に割る（単発 N 回に膨らませない）
  3. 429（日次クォータ枯渇）では単発フォールバックを一切行わない
  4. 分割の片側だけ失敗しても、成功した側の結果は捨てない
"""

import pytest

from app import embedding as emb_mod
from app.config import GOOGLE_EMBEDDING_MODEL, settings
from app.embedding import create_embeddings_batch


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeAsyncClient:
    """httpx.AsyncClient 互換のフェイク。queue の先頭から順に応答を返す。"""

    queue: list[FakeResponse] = []
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        FakeAsyncClient.calls.append({"url": url, "json": json})
        if not FakeAsyncClient.queue:
            raise AssertionError(
                "想定より多く API を呼んでいる（無料枠の浪費）。"
                f" これまでの呼び出し数={len(FakeAsyncClient.calls)}"
            )
        return FakeAsyncClient.queue.pop(0)


async def _no_sleep(_seconds):
    return None


@pytest.fixture
def google_provider(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "google")
    monkeypatch.setattr(settings, "google_generative_ai_api_key", "test-key")
    monkeypatch.setattr(settings, "embedding_model", GOOGLE_EMBEDDING_MODEL)
    monkeypatch.setattr(emb_mod.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(emb_mod.asyncio, "sleep", _no_sleep)
    FakeAsyncClient.queue = []
    FakeAsyncClient.calls = []
    return FakeAsyncClient


def _vec(n: int = 1536, value: float = 3.0) -> list[float]:
    return [value] * n


def _batch_ok(count: int) -> FakeResponse:
    return FakeResponse(200, {"embeddings": [{"values": _vec()} for _ in range(count)]})


def _count_batch_calls() -> int:
    return sum(1 for c in FakeAsyncClient.calls if ":batchEmbedContents" in c["url"])


def _count_single_calls() -> int:
    return sum(1 for c in FakeAsyncClient.calls if c["url"].endswith(":embedContent"))


async def test_long_text_is_truncated_before_batch_send(google_provider):
    """長文が混ざっていても事前に切り詰めて送るので 400 で全滅しない。

    これが本番事故の直接原因だった。切り詰めが無いと 1 件の長文が
    バッチ全体を 400 で巻き添えにし、100 件が単発 100 リクエストに化けた。
    """
    long_text = "あ" * 50_000
    google_provider.queue = [_batch_ok(2)]

    out = await create_embeddings_batch([long_text, "短いテキスト"])

    assert len(out) == 2
    assert out[0] is not None and out[1] is not None
    assert _count_batch_calls() == 1, "1 バッチ = 1 リクエストで済むはず"
    assert _count_single_calls() == 0, "単発フォールバックへ落ちてはいけない"

    sent = google_provider.calls[0]["json"]["requests"][0]["content"]["parts"][0]["text"]
    assert len(sent) == emb_mod._GEMINI_MAX_INPUT_CHARS
    assert len(sent) < len(long_text)


async def test_400_splits_batch_instead_of_falling_back_to_singles(google_provider):
    """400 はバッチを半分に割って絞り込む（単発 N 回に膨らませない）。

    4 件で 400 → 2 件ずつに分割し、両方成功。API 呼び出しは 3 回で済む。
    修正前の実装ではここが単発 4 回（＋バッチ 1 回）になっていた。
    """
    google_provider.queue = [
        FakeResponse(400, {"error": {"message": "input too long"}}),  # 4件バッチ
        _batch_ok(2),  # 左半分
        _batch_ok(2),  # 右半分
    ]

    out = await create_embeddings_batch(["a", "b", "c", "d"])

    assert len(out) == 4
    assert all(v is not None for v in out)
    assert _count_batch_calls() == 3
    assert _count_single_calls() == 0


async def test_400_on_single_item_delegates_to_single_path(google_provider):
    """1 件まで割っても 400 なら、半分に切り直す単発経路へ委ねる。"""
    google_provider.queue = [
        FakeResponse(400),  # 1件バッチが400
        FakeResponse(200, {"embedding": {"values": _vec()}}),  # 単発で成功
    ]

    out = await create_embeddings_batch(["only-one"])

    assert len(out) == 1 and out[0] is not None
    assert _count_batch_calls() == 1
    assert _count_single_calls() == 1


async def test_429_does_not_fall_back_to_singles(google_provider):
    """429（日次クォータ枯渇）では単発フォールバックを一切行わない。

    枯渇後に単発へ落ちても同じ 429 を貰い直すだけで、残りわずかな枠まで
    使い切ってしまう。_post_gemini のリトライ（4回）を消化した後は
    即座に打ち切って全件 None を返すのが正しい。
    """
    google_provider.queue = [FakeResponse(429) for _ in range(4)]  # リトライ上限まで429

    out = await create_embeddings_batch(["a", "b", "c"])

    assert out == [None, None, None]
    assert _count_single_calls() == 0, "429 の後に単発を撃ってはいけない"


async def test_429_stops_remaining_batches(google_provider):
    """429 を踏んだら以降のバッチも即打ち切る（枠を掘り続けない）。"""
    monkey_limit = 2
    original_limit = emb_mod._BATCH_SIZE_LIMIT
    emb_mod._BATCH_SIZE_LIMIT = monkey_limit
    try:
        google_provider.queue = [FakeResponse(429) for _ in range(4)]

        out = await create_embeddings_batch(["a", "b", "c", "d", "e", "f"])

        assert out == [None] * 6
        assert _count_batch_calls() == 4, "1バッチ目のリトライ4回だけで打ち切るはず"
        assert _count_single_calls() == 0
    finally:
        emb_mod._BATCH_SIZE_LIMIT = original_limit


async def test_partial_split_failure_keeps_successful_half(google_provider):
    """分割の片側だけ失敗しても、成功した側の結果は捨てない。

    クォータは既に消費済みなので、捨てると二重に損をする。
    """
    google_provider.queue = [
        FakeResponse(400),  # 4件バッチが400 → 分割
        _batch_ok(2),  # 左半分は成功
        FakeResponse(403),  # 右半分は復旧不能な失敗
        FakeResponse(403),  # 右半分の単発フォールバック1件目
        FakeResponse(403),  # 右半分の単発フォールバック2件目
    ]

    out = await create_embeddings_batch(["a", "b", "c", "d"])

    assert len(out) == 4
    assert out[0] is not None and out[1] is not None, "成功した左半分は残すこと"
    assert out[2] is None and out[3] is None
