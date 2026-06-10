from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://localhost:5432/knowhow"
    openai_api_key: str = ""
    kb_api_key: str = ""
    # HO-83 管理import専用キー（KB_API_KEY とは別系統）。未設定なら /api/admin/* は 503。
    admin_import_key: str = ""
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 1536
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


settings = Settings()
