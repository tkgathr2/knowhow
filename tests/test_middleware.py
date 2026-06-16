"""BrowserAuthMiddleware の境界挙動（fail-safe OFF / 保護 ON）を検証。"""
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from starlette.testclient import TestClient

from app import authn
from app import session as sess
from app.config import settings
from app.middleware import BrowserAuthMiddleware
from app.routers import auth_oauth


def build_app():
    app = FastAPI()
    app.add_middleware(BrowserAuthMiddleware)
    app.include_router(auth_oauth.router)

    @app.get("/")
    def root():
        return PlainTextResponse("home")

    @app.get("/growth")
    def growth():
        return PlainTextResponse("growth")

    @app.get("/api/stats")
    def stats():
        return PlainTextResponse("stats")

    @app.get("/health")
    def health():
        return PlainTextResponse("ok")
    return app


@pytest.fixture
def reset_settings():
    keep = (settings.google_client_id, settings.google_client_secret,
            settings.session_secret, settings.kb_api_key)
    yield
    (settings.google_client_id, settings.google_client_secret,
     settings.session_secret, settings.kb_api_key) = keep


def test_failsafe_open_when_oauth_disabled(reset_settings):
    settings.google_client_id = ""
    settings.google_client_secret = ""
    c = TestClient(build_app())
    assert c.get("/").status_code == 200
    assert c.get("/growth").status_code == 200
    assert c.get("/api/stats").status_code == 200


def test_protected_when_oauth_enabled(reset_settings):
    settings.google_client_id = "cid"
    settings.google_client_secret = "csec"
    settings.session_secret = "signing-secret-xyz"
    settings.kb_api_key = "agent-key-123"
    c = TestClient(build_app())

    # ページ未ログイン → /auth/login へリダイレクト
    r = c.get("/growth", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/auth/login" in r.headers["location"]

    # 保護API 未ログイン・鍵なし → 401
    assert c.get("/api/stats").status_code == 401
    # health は常に開放
    assert c.get("/health").status_code == 200

    # エージェント: 正しい X-API-Key → 通る
    assert c.get("/api/stats", headers={"X-API-Key": "agent-key-123"}).status_code == 200
    assert c.get("/api/stats", headers={"X-API-Key": "wrong"}).status_code == 401

    # ブラウザ: 有効なセッションcookie → 通る
    token = sess.make_session("ceo@takagi.bz", authn.session_secret(),
                              int(time.time()), 180 * 86400)
    c.cookies.set(authn.SESSION_COOKIE, token)
    assert c.get("/").status_code == 200
    assert c.get("/api/stats").status_code == 200
    me = c.get("/auth/me").json()
    assert me["user"] == "ceo@takagi.bz" and me["oauth_enabled"] is True


def test_koe_signals_read_is_protected(reset_settings):
    """GET /api/koe/signals は録音由来の機微情報（判断/リスク/人の不満/数字）。

    OAuth 有効時は digest/recordings と同様にログイン必須＝鍵なしブラウザは 401。
    （HIGH レビュー指摘の回帰防止：保護プレフィックス漏れを検知する）
    """
    settings.google_client_id = "cid"
    settings.google_client_secret = "csec"
    settings.session_secret = "signing-secret-xyz"
    settings.kb_api_key = "agent-key-123"
    c = TestClient(build_app())

    assert c.get("/api/koe/signals").status_code == 401
    assert c.get("/api/koe/digest").status_code == 401  # 既存の流儀と揃っていること
    assert c.get("/api/koe/signals", headers={"X-API-Key": "agent-key-123"}).status_code != 401
