from pydantic import model_validator
from pydantic_settings import BaseSettings

GOOGLE_EMBEDDING_MODEL = "gemini-embedding-001"
_OPENAI_EMBEDDING_MODEL = "text-embedding-3-large"


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://localhost:5432/knowhow"
    openai_api_key: str = ""
    kb_api_key: str = ""
    # HO-83 管理import専用キー（KB_API_KEY とは別系統）。未設定なら /api/admin/* は 503。
    admin_import_key: str = ""
    embedding_model: str = _OPENAI_EMBEDDING_MODEL
    embedding_dim: int = 1536
    # --- embedding プロバイダ（2026-07-25 Google無料枠へ移行・残高切れ事故の構造的排除）---
    # "google" | "openai" | "" = 自動（Googleキーがあれば google、なければ openai）。
    # ロールバックは EMBEDDING_PROVIDER=openai を設定するだけ。
    embedding_provider: str = ""
    google_generative_ai_api_key: str = ""
    github_webhook_secret: str = ""

    # --- Google ログイン（ブラウザ向け・6か月セッション）---
    # 3つ（client_id / client_secret / 署名鍵）が揃うと認証ON。未設定なら全開放のまま。
    google_client_id: str = ""
    google_client_secret: str = ""
    # セッション署名鍵。未設定なら KB_API_KEY を流用する（authn.session_secret）。
    session_secret: str = ""
    # 本番の公開URL（redirect_uri 用）。例: https://knowhow.up.railway.app
    public_base_url: str = ""
    # アクセスを許可するメールドメイン/メール（カンマ区切り）。
    allowed_email_domains: str = "takagi.bz,positive-z.co.jp"
    allowed_emails: str = ""
    # 一度ログインしたら再認証不要な期間（既定180日＝6か月）。
    session_max_age_days: int = 180

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def active_embedding_provider(self) -> str:
        """"google" | "openai" | "none"。明示指定が最優先、無指定はキーの有無で自動解決。"""
        if self.embedding_provider in ("google", "openai"):
            return self.embedding_provider
        if self.google_generative_ai_api_key:
            return "google"
        if self.openai_api_key:
            return "openai"
        return "none"

    @model_validator(mode="after")
    def _resolve_embedding_model(self):
        # provider=google なのに embedding_model が OpenAI 既定値のままなら Gemini に解決する。
        # これにより ingest 各所の「settings.embedding_model を行にスタンプする」既存コードが
        # 無修正で正しいモデル名を記録する（EMBEDDING_MODEL を明示した場合はそれを尊重）。
        if self.active_embedding_provider == "google" and self.embedding_model == _OPENAI_EMBEDDING_MODEL:
            self.embedding_model = GOOGLE_EMBEDDING_MODEL
        return self


settings = Settings()
