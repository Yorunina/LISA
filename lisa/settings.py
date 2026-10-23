from decimal import Decimal
from pathlib import Path

from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LISA_", env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    db_host: str = "127.0.0.1"
    db_port: int = 55432
    db_name: str = "lisa"
    db_user: str = "lisa"
    db_password: SecretStr
    session_days: int = Field(default=14, ge=1, le=90)
    cookie_secure: bool = False  # Enable behind HTTPS in deployment.
    onebot_ws_url: str = "ws://127.0.0.1:3001"
    onebot_token: SecretStr = SecretStr("")
    qq_test_user: str | None = None
    web_url: str = "http://127.0.0.1:8000"
    model_base_url: str = "https://test.mcii.cc/v1"
    model_api_key: SecretStr = SecretStr("")
    model_name: str = "gpt-6-astra"
    model_daily_usd: Decimal = Field(default=Decimal("0.50"), gt=0)
    model_task_usd: Decimal = Field(default=Decimal("0.15"), gt=0)
    model_input_per_million: Decimal = Field(default=Decimal("10"), gt=0)
    model_output_per_million: Decimal = Field(default=Decimal("50"), gt=0)
    model_max_output_tokens: int = Field(default=600, ge=64, le=4096)
    model_concurrency: int = Field(default=1, ge=1, le=8)
    model_timeout_seconds: float = Field(default=60, gt=0, le=180)
    embedding_model: str = ""
    embedding_dimensions: int = Field(default=1536, ge=1, le=4096)
    # Placeholder: configure the provider's positive input price before enabling embeddings.
    embedding_input_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    chat_enabled: bool = False
    chat_test_enabled: bool = False
    writing_max_chars: int = Field(default=600, ge=100, le=2000)
    writing_output_tokens: int = Field(default=1200, ge=256, le=4096)
    autonomy_enabled: bool = False
    proactive_enabled: bool = False
    autonomous_scene_interval_minutes: int = Field(default=120, ge=1, le=1440)
    autonomous_scene_limit: int = Field(default=2, ge=1, le=10)

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    @property
    def conninfo(self) -> str:
        return make_conninfo(
            host=self.db_host,
            port=self.db_port,
            dbname=self.db_name,
            user=self.db_user,
            password=self.db_password.get_secret_value(),
            connect_timeout=5,
        )
