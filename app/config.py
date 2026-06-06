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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
