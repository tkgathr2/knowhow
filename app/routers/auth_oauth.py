"""Google ログインのエンドポイント群（/auth/*）。

/auth/login    → state を発行して Google のログイン画面へ
/auth/callback → コード交換→userinfo→許可判定→180日セッションcookie発行
/auth/logout   → セッションcookie破棄
/auth/me       → 現在のログインユーザー（ダッシュボードの表示用）
"""

from __future__ import annotations

import secrets
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import authn, oauth
from app import session as sess
from app.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])

_STATE_TTL = 600  # state cookie は10分で失効


def _base_url(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


@router.get("/login")
async def login(request: Request, next: str = "/"):
    if not authn.oauth_enabled():
        raise HTTPException(status_code=503, detail="Googleログインは未設定です")
    state = secrets.token_urlsafe(24)
    now = int(time.time())
    state_token = sess.sign(
        {"s": state, "next": oauth.safe_next(next), "exp": now + _STATE_TTL},
        authn.session_secret(),
    )
    url = oauth.authorize_url(
        settings.google_client_id, oauth.redirect_uri_for(_base_url(request)), state
    )
    resp = RedirectResponse(url)
    resp.set_cookie(
        authn.STATE_COOKIE, state_token, max_age=_STATE_TTL,
        httponly=True, secure=True, samesite="lax",
    )
    return resp


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    if not authn.oauth_enabled():
        raise HTTPException(status_code=503, detail="Googleログインは未設定です")
    now = int(time.time())
    data = sess.verify(request.cookies.get(authn.STATE_COOKIE), authn.session_secret(), now)
    if not data or data.get("s") != state or not state:
        raise HTTPException(status_code=400, detail="state検証に失敗しました")

    redirect_uri = oauth.redirect_uri_for(_base_url(request))
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            oauth.GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=401, detail="トークン交換に失敗しました")
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=401, detail="アクセストークンが取得できません")
        userinfo_resp = await client.get(
            oauth.GOOGLE_USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(status_code=401, detail="ユーザー情報の取得に失敗しました")
        info = userinfo_resp.json()

    email = info.get("email")
    if (
        not email
        or not info.get("email_verified", False)
        or not oauth.email_allowed(
            email, settings.allowed_email_domains, settings.allowed_emails
        )
    ):
        raise HTTPException(
            status_code=403, detail="このGoogleアカウントはアクセスを許可されていません"
        )

    max_age = settings.session_max_age_days * 86400
    token = sess.make_session(email, authn.session_secret(), now, max_age)
    resp = RedirectResponse(oauth.safe_next(data.get("next")))
    resp.set_cookie(
        authn.SESSION_COOKIE, token, max_age=max_age,
        httponly=True, secure=True, samesite="lax",
    )
    resp.delete_cookie(authn.STATE_COOKIE)
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/")
    resp.delete_cookie(authn.SESSION_COOKIE)
    return resp


@router.get("/me")
async def me(request: Request):
    return JSONResponse(
        {"user": getattr(request.state, "user", None), "oauth_enabled": authn.oauth_enabled()}
    )
