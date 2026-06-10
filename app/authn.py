"""OAuth の有効判定とセッション署名鍵の解決（router/middleware で共用）。

fail-safe: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET と署名鍵が揃って初めて
認証が有効になる。未設定なら現行の「全開放」挙動を維持するので、資格情報を
入れる前にデプロイしても何も壊れない（KB_API_KEY と同じ段階リリース方式）。
"""

from __future__ import annotations

import hmac

from app.config import settings

SESSION_COOKIE = "kh_session"
STATE_COOKIE = "kh_oauth_state"


def session_secret() -> str:
    """セッション署名鍵。専用 SESSION_SECRET が無ければ KB_API_KEY を流用する。"""
    return settings.session_secret or settings.kb_api_key


def oauth_enabled() -> bool:
    return bool(
        settings.google_client_id
        and settings.google_client_secret
        and session_secret()
    )


def valid_api_key(x_api_key: str | None) -> bool:
    """エージェント/Devin 用の X-API-Key 認証（ブラウザ以外の経路を生かす）。"""
    expected = settings.kb_api_key
    if not expected or not x_api_key:
        return False
    return hmac.compare_digest(x_api_key, expected)
