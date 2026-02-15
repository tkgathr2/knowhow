from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://localhost:5432/knowhow"
    openai_api_key: str = ""
    kb_api_key: str = ""
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 3072

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
