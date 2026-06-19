"""ブラウザ向けアクセスを Google ログインで保護するミドルウェア。

- 認証OFF（資格情報未設定）の間は全リクエストを素通り＝現行の開放挙動を維持。
- 認証ONのとき:
    * HTMLページ（/ と /growth）= 未ログインなら /auth/login へリダイレクト
    * ダッシュボードが使う read API = 未ログイン かつ X-API-Key 無しなら 401
    * /health /auth /static = 常に開放（ログイン動線・静的資産）
    * write系 /api/* は各ルータの X-API-Key 依存がそのまま担当（ここでは触らない）
エージェント/Devin は X-API-Key を持つので認証ON後も従来どおり API を使える。
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from app import authn
from app import session as sess

_PAGE_PATHS = {"/", "/growth", "/daily", "/token-cutter", "/anthropic-cost", "/cost-cutter", "/lore"}
_PROTECTED_API_PREFIXES = (
    "/api/stats",
    "/api/growth",
    "/api/token-cutter/stats",
    "/api/anthropic-cost/stats",
    "/api/cost-cutter/stats",
    "/api/recent",
    "/api/tags",
    "/api/chunks",
    "/api/search",
    "/api/devin/recall",
    # ロア（録音資産）の read：録音内容は機微なのでブラウザは Google ログイン必須
    # （GET /koe/digest・/koe/recordings・/koe/signals）。POST/PATCH 系は koe 側の
    # X-API-Key ガードが別途効く。signals は判断/リスク/人の不満/数字＝録音以上に機微。
    "/api/koe/digest",
    "/api/koe/recordings",
    "/api/koe/signals",
    "/api/koe/briefing",
)
_ALWAYS_OPEN_PREFIXES = ("/health", "/auth", "/static")


class BrowserAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        request.state.user = None

        if not authn.oauth_enabled():
            return await call_next(request)

        token = request.cookies.get(authn.SESSION_COOKIE)
        data = sess.verify(token, authn.session_secret(), int(time.time())) if token else None
        if data:
            request.state.user = data.get("email")

        if any(path.startswith(p) for p in _ALWAYS_OPEN_PREFIXES):
            return await call_next(request)

        is_page = path in _PAGE_PATHS
        is_protected_api = any(path.startswith(p) for p in _PROTECTED_API_PREFIXES)
        if (is_page or is_protected_api) and not request.state.user:
            if authn.valid_api_key(request.headers.get("X-API-Key")):
                return await call_next(request)
            if is_page:
                return RedirectResponse(f"/auth/login?next={path}")
            return JSONResponse({"detail": "認証が必要です（Googleログイン）"}, status_code=401)

        return await call_next(request)
