"""Google Gemini embedding 移行（2026-07-25）の単体テスト。

外部 API は叩かず httpx.AsyncClient をフェイクに差し替える。
検証する契約：
  - 1536 次元指定＋L2 正規化（Gemini は 3072 以外を正規化せず返すため）
  - 保存側= RETRIEVAL_DOCUMENT / クエリ側= RETRIEVAL_QUERY の非対称タスク型
  - 入力長超過(400)は本文を半分に切って再試行
  - 429/5xx は指数バックオフ後に成功すれば値を返す
  - 失敗時は None（呼び出し側の failed_embedding 契約を壊さない）
  - バッチは入力順を保ち、バッチ全体失敗時は単発フォールバック
"""

import math

import pytest

from app import embedding as emb_mod
from app.config import GOOGLE_EMBEDDING_MODEL, Settings, settings
from app.embedding import create_embedding, create_embeddings_batch, l2_normalize


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
        return FakeAsyncClient.queue.pop(0)


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


async def _no_sleep(_seconds):
    return None


def _vec(n: int = 1536, value: float = 3.0) -> list[float]:
    return [value] * n


def test_l2_normalize_unit_norm():
    out = l2_normalize(_vec(4, 3.0))
    norm = math.sqrt(sum(v * v for v in out))
    assert abs(norm - 1.0) < 1e-9


def test_l2_normalize_zero_vector_passthrough():
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_settings_auto_resolves_google_model():
    s = Settings(
        _env_file=None,
        google_generative_ai_api_key="k",
        openai_api_key="",
    )
    assert s.active_embedding_provider == "google"
    assert s.embedding_model == GOOGLE_EMBEDDING_MODEL


def test_settings_no_keys_means_none():
    s = Settings(_env_file=None, google_generative_ai_api_key="", openai_api_key="")
    assert s.active_embedding_provider == "none"


async def test_no_provider_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "")
    monkeypatch.setattr(settings, "google_generative_ai_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert await create_embedding("hello") is None


async def test_google_document_success(google_provider):
    google_provider.queue = [FakeResponse(200, {"embedding": {"values": _vec()}})]
    out = await create_embedding("本文テキスト")
    assert out is not None and len(out) == 1536
    assert abs(math.sqrt(sum(v * v for v in out)) - 1.0) < 1e-6  # 正規化済み
    body = google_provider.calls[0]["json"]
    assert body["taskType"] == "RETRIEVAL_DOCUMENT"
    assert body["outputDimensionality"] == 1536
    assert ":embedContent" in google_provider.calls[0]["url"]


async def test_google_query_task_type(google_provider):
    google_provider.queue = [FakeResponse(200, {"embedding": {"values": _vec()}})]
    out = await create_embedding("検索クエリ", task="query")
    assert out is not None
    assert google_provider.calls[0]["json"]["taskType"] == "RETRIEVAL_QUERY"


async def test_google_400_halves_text_and_retries(google_provider):
    long_text = "あ" * 1000
    google_provider.queue = [
        FakeResponse(400, {"error": {"message": "input too long"}}),
        FakeResponse(200, {"embedding": {"values": _vec()}}),
    ]
    out = await create_embedding(long_text)
    assert out is not None
    sent_first = google_provider.calls[0]["json"]["content"]["parts"][0]["text"]
    sent_second = google_provider.calls[1]["json"]["content"]["parts"][0]["text"]
    assert len(sent_first) == 1000
    assert len(sent_second) == 500


async def test_google_429_backoff_then_success(google_provider):
    google_provider.queue = [
        FakeResponse(429),
        FakeResponse(200, {"embedding": {"values": _vec()}}),
    ]
    out = await create_embedding("rate limited once")
    assert out is not None
    assert len(google_provider.calls) == 2


async def test_google_hard_failure_returns_none(google_provider):
    google_provider.queue = [FakeResponse(403, {"error": {"message": "denied"}})]
    assert await create_embedding("denied") is None


async def test_batch_preserves_order(google_provider):
    google_provider.queue = [
        FakeResponse(
            200,
            {"embeddings": [{"values": _vec(value=1.0)}, {"values": _vec(value=2.0)}]},
        )
    ]
    out = await create_embeddings_batch(["one", "two"])
    assert len(out) == 2
    assert out[0] is not None and out[1] is not None
    assert ":batchEmbedContents" in google_provider.calls[0]["url"]
    reqs = google_provider.calls[0]["json"]["requests"]
    assert [r["content"]["parts"][0]["text"] for r in reqs] == ["one", "two"]
    assert all(r["taskType"] == "RETRIEVAL_DOCUMENT" for r in reqs)


async def test_batch_failure_falls_back_to_singles(google_provider):
    google_provider.queue = [
        FakeResponse(403),  # バッチ全体が失敗（バックオフ対象外の即時失敗）
        FakeResponse(200, {"embedding": {"values": _vec()}}),  # 単発1件目
        FakeResponse(403),  # 単発2件目は失敗 → None
    ]
    out = await create_embeddings_batch(["a", "b"])
    assert out[0] is not None
    assert out[1] is None


# --- 2026-07-25 bug-check-lab 再現テスト（本体は未修正のため red のまま） -----------
#
# 北村（ロジック）指摘: _post_gemini は client.post() を try/except で囲んでおらず、
# HTTPステータスとして返る 429/5xx にしかバックオフが効かない。本番で実際に観測した
# 障害（Gemini無料枠クォータ枯渇時、応答が60〜100秒以上返らずRailwayが502を返す）は
# httpx側のタイムアウト例外として現れるため、このバックオフ機構は無力どころか、
# 呼び出し元（admin_reembed / create_embedding 経由の /search /devin/recall 等）まで
# 例外がそのまま伝播してしまう。


class RaisingAsyncClient(FakeAsyncClient):
    """post() が例外を投げるフェイク（httpx のタイムアウト/接続断を模倣）。"""

    exc_to_raise: BaseException = RuntimeError("boom")

    async def post(self, url, json=None, headers=None):
        raise RaisingAsyncClient.exc_to_raise


async def test_post_gemini_timeout_is_not_caught_bug_reproduction(monkeypatch):
    """再現テスト: httpx がタイムアウト例外を送出すると create_embedding は
    None を返さず例外をそのまま呼び出し元へ伝播させてしまう（failed_embedding
    契約違反）。本体修正後はこの assert が「例外は起きず None が返る」に変わる想定。
    現状は Critical バグの実証のため意図的に red。
    """
    import httpx

    monkeypatch.setattr(settings, "embedding_provider", "google")
    monkeypatch.setattr(settings, "google_generative_ai_api_key", "test-key")
    monkeypatch.setattr(settings, "embedding_model", GOOGLE_EMBEDDING_MODEL)
    monkeypatch.setattr(emb_mod.httpx, "AsyncClient", RaisingAsyncClient)
    monkeypatch.setattr(emb_mod.asyncio, "sleep", _no_sleep)
    RaisingAsyncClient.exc_to_raise = httpx.ReadTimeout("simulated Gemini hang")

    # 期待される安全な契約（他の create_embedding 失敗系テストと同じく None を返す）
    # だが現状の実装はここで httpx.ReadTimeout をそのまま外へ投げるため、
    # このテストは「例外が伝播しないこと」を検証して red になる。
    result = await create_embedding("this should not hang the caller")
    assert result is None, (
        "create_embedding は失敗時に None を返す契約のはずだが、"
        "_post_gemini が client.post() の例外を捕捉していないため、"
        "httpx.ReadTimeout 等がそのまま呼び出し元(admin_reembed/search/devin.recall)まで伝播する"
    )


async def test_diagnose_reports_quota_exhausted_not_missing_key(google_provider):
    """429 は「キー未設定」ではなく quota_exhausted として報告する。

    2026-07-26 の朝パトロールで Gemini の embed_content 無料枠(1000件/日)を
    使い切って 429 が返っていたのに /health/embedding が
    "API key not configured" と表示し、原因の切り分けが遠回りになった。
    設定ミスと課金枯渇は対処がまったく違うので同じ文言にしない。
    """
    google_provider.queue = [
        FakeResponse(429, {"error": {"message": "You exceeded your current quota"}})
    ]

    diag = await emb_mod.diagnose_embedding()

    assert diag["ok"] is False
    assert diag["reason"] == "quota_exhausted"
    assert diag["httpStatus"] == 429
    assert "quota" in diag["detail"]


async def test_diagnose_does_not_retry_on_429(google_provider):
    """監視は現時点の生の状態を最短で知りたいのでリトライしない。"""
    google_provider.queue = [FakeResponse(429, {"error": {"message": "quota"}})]

    await emb_mod.diagnose_embedding()

    assert len(google_provider.calls) == 1


async def test_diagnose_ok_when_gemini_returns_vector(google_provider):
    google_provider.queue = [FakeResponse(200, {"embedding": {"values": _vec()}})]

    diag = await emb_mod.diagnose_embedding()

    assert diag["ok"] is True
    assert diag["reason"] == "ok"


async def test_diagnose_reports_no_api_key_when_provider_none(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "")
    monkeypatch.setattr(settings, "google_generative_ai_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")

    diag = await emb_mod.diagnose_embedding()

    assert diag["ok"] is False
    assert diag["reason"] == "no_api_key"
