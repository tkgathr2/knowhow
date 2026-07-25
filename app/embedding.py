import asyncio
import math

import httpx
from openai import AsyncOpenAI

from app.config import settings

_openai_client: AsyncOpenAI | None = None

# Gemini embedContent の入力上限は 2,048 トークン。超過時は 400 が返るため
# 半分に切って再試行する（_embed_google の 400 ハンドリング参照）。
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_GEMINI_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BATCH_SIZE_LIMIT = 100  # batchEmbedContents の API 上限
_CIRCUIT_BREAKER_THRESHOLD = 5  # バッチ内フォールバックが連続失敗した回数の上限


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


def l2_normalize(vec: list[float]) -> list[float]:
    # gemini-embedding-001 は 3072 次元以外を正規化せずに返すため、
    # 保存前に必ず L2 正規化する（Google 公式ドキュメントの指示）。
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


async def _post_gemini(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response | None:
    """429/5xx、および httpx のタイムアウト/接続エラーを指数バックオフで再試行する。

    タイムアウト(httpx.TimeoutException)や接続断・DNS失敗等(httpx.TransportError)は
    HTTPステータスとして返ってこないため、ステータスコードによる分岐だけでは検知できない。
    ここで捕捉し、429/5xx と同様にバックオフ→再試行の対象にする。
    全リトライを使い果たしてもなお失敗した場合は None を返し、呼び出し元に失敗を伝える
    （呼び出し元は None を素通しして create_embedding の None 契約を維持する）。
    """
    resp: httpx.Response | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await client.post(
                url,
                json=payload,
                headers={"x-goog-api-key": settings.google_generative_ai_api_key},
            )
        except (httpx.TimeoutException, httpx.TransportError):
            resp = None
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(2 ** attempt * 5)  # 5s, 10s, 20s
                continue
            return None
        if resp.status_code not in (429, 500, 502, 503):
            return resp
        if attempt < _MAX_RETRIES:
            await asyncio.sleep(2 ** attempt * 5)  # 5s, 10s, 20s
    return resp


async def _embed_google(text_value: str) -> list[float] | None:
    url = f"{_GEMINI_BASE}/{settings.embedding_model}:embedContent"
    text = text_value
    async with httpx.AsyncClient(timeout=_GEMINI_TIMEOUT) as client:
        for _ in range(3):  # 入力長超過(400)は半分に切って再試行
            payload = {
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": settings.embedding_dim,
                "taskType": "RETRIEVAL_DOCUMENT",
            }
            resp = await _post_gemini(client, url, payload)
            if resp is None:
                return None
            if resp.status_code == 200:
                values = resp.json().get("embedding", {}).get("values")
                if not values:
                    return None
                return l2_normalize(values)
            if resp.status_code == 400 and len(text) > 200:
                text = text[: len(text) // 2]
                continue
            return None
    return None


async def _embed_google_query(text_value: str) -> list[float] | None:
    url = f"{_GEMINI_BASE}/{settings.embedding_model}:embedContent"
    async with httpx.AsyncClient(timeout=_GEMINI_TIMEOUT) as client:
        payload = {
            "content": {"parts": [{"text": text_value[:4000]}]},
            "outputDimensionality": settings.embedding_dim,
            "taskType": "RETRIEVAL_QUERY",
        }
        resp = await _post_gemini(client, url, payload)
        if resp is None:
            return None
        if resp.status_code == 200:
            values = resp.json().get("embedding", {}).get("values")
            if not values:
                return None
            return l2_normalize(values)
        return None


async def create_embedding(text_value: str, *, task: str = "document") -> list[float] | None:
    """テキストを 1536 次元ベクトルにする。失敗時は None（呼び出し側契約を維持）。

    task: "document"（保存側・既定）| "query"（検索クエリ側）。
    Gemini では非対称タスク型（RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY）を使う。
    OpenAI 経路では task は無視される。
    """
    provider = settings.active_embedding_provider
    if provider == "google":
        if task == "query":
            return await _embed_google_query(text_value)
        return await _embed_google(text_value)
    if provider == "openai":
        client = _get_openai_client()
        resp = await client.embeddings.create(
            model=settings.embedding_model,
            input=text_value,
            dimensions=settings.embedding_dim,
        )
        return resp.data[0].embedding
    return None


async def create_embeddings_batch(texts: list[str]) -> list[list[float] | None]:
    """複数テキストを一括 embedding（reembed 用）。入力順と同順で返す。

    Google: batchEmbedContents（N 件 = 1 リクエスト）で無料枠の日次リクエスト上限を節約。
    バッチ全体が失敗したら 1 件ずつの単発呼び出しにフォールバックする
    （1 件の不正入力がバッチ全滅を招くのを防ぐ）。
    """
    provider = settings.active_embedding_provider
    if provider != "google":
        return [await create_embedding(t) for t in texts]

    results: list[list[float] | None] = []
    url = f"{_GEMINI_BASE}/{settings.embedding_model}:batchEmbedContents"
    model_ref = f"models/{settings.embedding_model}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        for start in range(0, len(texts), _BATCH_SIZE_LIMIT):
            batch = texts[start : start + _BATCH_SIZE_LIMIT]
            payload = {
                "requests": [
                    {
                        "model": model_ref,
                        "content": {"parts": [{"text": t if t else " "}]},
                        "outputDimensionality": settings.embedding_dim,
                        "taskType": "RETRIEVAL_DOCUMENT",
                    }
                    for t in batch
                ]
            }
            resp = await _post_gemini(client, url, payload)
            if resp is not None and resp.status_code == 200:
                embeddings = resp.json().get("embeddings", [])
                for i in range(len(batch)):
                    values = embeddings[i].get("values") if i < len(embeddings) else None
                    results.append(l2_normalize(values) if values else None)
            else:
                # バッチ失敗 → 単発フォールバック（長文 400 の切り詰め再試行も効く）
                # ただし連続失敗が閾値を超えたらサーキットブレーカーを開き、
                # 残り全件を即 None 確定して打ち切る（クォータ枯渇時に
                # 1件ずつ遅いリトライを繰り返して数時間かかる事態を防ぐ）。
                consecutive_failures = 0
                circuit_open = False
                for t in batch:
                    if circuit_open:
                        results.append(None)
                        continue
                    single = await _embed_google(t)
                    results.append(single)
                    if single is None:
                        consecutive_failures += 1
                        if consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
                            circuit_open = True
                    else:
                        consecutive_failures = 0
    return results


async def diagnose_embedding() -> dict:
    """監視用: embedding を1回だけ実測し、失敗の「本当の理由」まで返す。

    create_embedding は契約上どんな失敗でも None を返すため、監視側からは
    「キー未設定」も「無料枠の使い切り(429)」も「入力長超過(400)」も
    区別がつかない。実際 2026-07-26 の朝パトロールで Gemini の
    embed_content 無料枠(1000件/日)を使い切って 429 が返っていたのに
    /health/embedding は "API key not configured" と表示し、原因究明が
    遠回りになった。ここでは HTTP ステータスと API のエラーメッセージを
    そのまま返して誤診を防ぐ。

    リトライはしない（監視は現時点の生の状態を最短で知りたいため）。
    """
    provider = settings.active_embedding_provider
    if provider == "none":
        return {"ok": False, "reason": "no_api_key", "detail": "embedding provider 未設定"}

    if provider == "openai":
        try:
            vector = await create_embedding("health check")
        except Exception as exc:  # noqa: BLE001 - 失敗理由をそのまま監視に出す
            return {"ok": False, "reason": "api_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
        if vector is None or len(vector) != settings.embedding_dim:
            return {"ok": False, "reason": "api_error", "detail": "embedding が返らなかった"}
        return {"ok": True, "reason": "ok", "detail": ""}

    url = f"{_GEMINI_BASE}/{settings.embedding_model}:embedContent"
    payload = {
        "content": {"parts": [{"text": "health check"}]},
        "outputDimensionality": settings.embedding_dim,
        "taskType": "RETRIEVAL_DOCUMENT",
    }
    try:
        async with httpx.AsyncClient(timeout=_GEMINI_TIMEOUT) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"x-goog-api-key": settings.google_generative_ai_api_key},
            )
    except Exception as exc:  # noqa: BLE001 - タイムアウト/接続断もそのまま出す
        return {"ok": False, "reason": "network_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}

    if resp.status_code == 200:
        values = resp.json().get("embedding", {}).get("values")
        if values and len(values) == settings.embedding_dim:
            return {"ok": True, "reason": "ok", "detail": ""}
        return {"ok": False, "reason": "api_error", "detail": "200 だが values が空/次元不一致"}

    reason = "quota_exhausted" if resp.status_code == 429 else "api_error"
    try:
        detail = resp.json().get("error", {}).get("message", "")
    except Exception:  # noqa: BLE001
        detail = resp.text
    return {"ok": False, "reason": reason, "httpStatus": resp.status_code, "detail": detail[:300]}


def escape_like(query: str) -> str:
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clamp_top_k(top_k: int, *, low: int = 1, high: int = 100) -> int:
    return max(low, min(top_k, high))
