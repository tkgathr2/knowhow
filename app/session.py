"""自前の署名付きセッションクッキー（外部依存なし）。

itsdangerous や starlette SessionMiddleware を足さずに、標準ライブラリの
hmac/base64/json だけで「改ざん不可・有効期限つき」のセッショントークンを作る。
6か月（180日）維持は exp を発行時刻+180日にして cookie の max_age と揃えるだけ。

now を引数で受け取る純粋関数にして、時刻に依存せず単体テストできるようにする（神谷）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sig(body: str, secret: str) -> str:
    return _b64e(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def sign(payload: dict, secret: str) -> str:
    """payload(dict) を base64(json).署名 の文字列にする。"""
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_sig(body, secret)}"


def verify(token: str | None, secret: str, now_ts: int) -> dict | None:
    """署名と有効期限(exp)を検証。OKなら payload、ダメなら None。"""
    if not token or "." not in token or not secret:
        return None
    body, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, _sig(body, secret)):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    exp = payload.get("exp")
    if exp is None or now_ts >= int(exp):
        return None
    return payload


def make_session(email: str, secret: str, issued_ts: int, max_age_s: int) -> str:
    """ログイン済みユーザーのセッショントークンを発行する。"""
    return sign({"email": email, "iat": issued_ts, "exp": issued_ts + max_age_s}, secret)
