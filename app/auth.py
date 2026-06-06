"""API key authentication dependency.

後方互換: 認証は KB_API_KEY が設定されている時だけ有効になる。
KB_API_KEY が空（現在の本番状態）なら全リクエストを通すので、本変更を
デプロイしても既存の呼び出し元（Devin / ダッシュボード等）は壊れない。
KB_API_KEY を環境変数にセットした瞬間から、保護対象エンドポイントは
一致する X-API-Key ヘッダーを要求する。

タイミング攻撃を避けるため比較は hmac.compare_digest を使う（神谷）。
GitHub Webhook は別系統（HMAC 署名）で守るため、本依存は付けない。
"""

import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    expected = settings.kb_api_key
    if not expected:
        # 認証無効（キー未設定）。現行のオープン挙動を維持する。
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
