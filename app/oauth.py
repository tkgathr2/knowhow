"""Google OAuth 2.0（Authorization Code フロー）の純粋ヘルパー。

Authlib 等を足さず httpx だけで Google にコード交換する。ここには副作用の無い
URL組み立て・許可判定だけを置き、HTTP通信は router 側で行う（テスト容易性）。
"""

from __future__ import annotations

from urllib.parse import urlencode

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"


def parse_csv(value: str | None) -> list[str]:
    return [x.strip().lower() for x in (value or "").split(",") if x.strip()]


def email_allowed(email: str | None, domains_csv: str, emails_csv: str) -> bool:
    """許可メール/許可ドメインに一致すれば True。何も設定が無ければ安全側で False。"""
    if not email:
        return False
    email = email.lower()
    allow_emails = parse_csv(emails_csv)
    if email in allow_emails:
        return True
    domains = parse_csv(domains_csv)
    if not domains and not allow_emails:
        return False
    domain = email.rsplit("@", 1)[-1]
    return domain in domains


def redirect_uri_for(base_url: str) -> str:
    return base_url.rstrip("/") + "/auth/callback"


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Google のログイン画面へ送る authorize URL を組み立てる。"""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    return f"{GOOGLE_AUTH_ENDPOINT}?{query}"


def safe_next(next_path: str | None) -> str:
    """オープンリダイレクト対策：自サイト内の絶対パスだけ許可。"""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path
