from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LISA_",
        env_file=ROOT / "runtime" / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
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
    # An explicit deployment allowlist, independent from legacy group-management is_admin.
    operations_maintainers: str = ""
    operations_timezone: str = "Asia/Shanghai"
    operations_refresh_seconds: int = Field(default=30, ge=10, le=300)
    operations_heartbeat_seconds: int = Field(default=20, ge=5, le=60)
    operations_stale_seconds: int = Field(default=90, ge=30, le=600)
    operations_task_warning_seconds: int = Field(default=300, ge=30, le=3600)
    operations_retention_days: int = Field(default=30, ge=1, le=365)
    # The probe runs on the server; the management link is opened on the maintainer's device.
    napcat_webui_probe_url: str = "http://127.0.0.1:6099/webui"
    napcat_webui_url: str = "http://127.0.0.1:6099/webui"
    # Already persisted by the local dependency initializer; never returned in status views.
    napcat_webui_token: SecretStr = SecretStr("")

    @field_validator("napcat_webui_probe_url", "napcat_webui_url")
    @classmethod
    def safe_napcat_webui_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"} or not url.hostname
            or url.username is not None or url.password is not None
            or url.query or url.fragment or any(ord(char) < 33 for char in value)
        ):
            raise ValueError("NapCat WebUI URL must be HTTP(S), without credentials or tokens")
        # Force validation of malformed/out-of-range ports too.
        url.port
        return value

    @field_validator("operations_timezone")
    @classmethod
    def valid_operations_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown operations timezone") from exc
        return value

    @field_validator("web_url")
    @classmethod
    def valid_web_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("web_url must be an absolute HTTP(S) base URL")
        return value

    model_base_url: str = "https://drc.totokens.cc/v1"
    model_api_key: SecretStr = SecretStr("")
    model_name: str = "gpt-6.1-sol"
    model_api_protocol: Literal["responses", "chat_completions"] = "responses"
    model_reasoning_effort: Literal["low", "medium", "high"] | None = None
    dialogue_skill_mode: Literal["off", "core", "examples", "corpus"] = "off"
    # Legacy research estimate; normal conversational schemas use provider capacity.
    dialogue_skill_output_tokens: int = Field(default=2400, ge=600, le=4096)

    @field_validator("model_reasoning_effort", mode="before")
    @classmethod
    def empty_reasoning_effort(cls, value):
        return None if value == "" else value

    # Amounts use CNY; legacy *_usd names remain compatible with stored data and clients.
    model_daily_usd: Decimal = Field(default=Decimal("10.00"), ge=0)
    model_task_usd: Decimal = Field(default=Decimal("0.15"), ge=0)
    model_input_per_million: Decimal = Field(default=Decimal("2"), ge=0)
    model_output_per_million: Decimal = Field(default=Decimal("10"), ge=0)

    @field_validator("model_input_per_million", "model_output_per_million", mode="before")
    @classmethod
    def unknown_model_price(cls, value):
        return Decimal(0) if value is None or value == "" else value

    # Conservative application cap in serialized UTF-8 bytes. The provider
    # model supports a much larger token window; this protects cost and latency.
    model_max_input_bytes: int = Field(default=64_000, ge=16_000, le=4_194_304)
    model_max_output_tokens: int = Field(default=600, ge=64, le=4096)
    model_concurrency: int = Field(default=1, ge=1, le=8)
    model_timeout_seconds: float = Field(default=120, gt=0, le=180)
    # Independent image provider. Consumption is recorded separately from text quotas.
    image_model: str = ""
    image_base_url: str = ""
    image_api_key: SecretStr = SecretStr("")
    image_timeout_seconds: float = Field(default=180, gt=0, le=600)
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_dimensions: int = Field(default=1536, ge=1, le=4096)
    embedding_batch_size: int = Field(default=32, ge=1, le=32)
    # Placeholder: configure the provider's positive input price before enabling embeddings.
    embedding_input_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    vision_model: str = ""
    vision_base_url: str = ""
    vision_api_key: SecretStr = SecretStr("")
    vision_input_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    vision_output_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    chat_enabled: bool = False
    chat_test_enabled: bool = False
    writing_max_chars: int = Field(default=600, ge=100, le=2000)
    writing_output_tokens: int = Field(default=1200, ge=256, le=4096)
    style_max_files: int = Field(default=16, ge=1, le=64)
    style_max_upload_bytes: int = Field(default=8_388_608, ge=1024, le=33_554_432)
    style_corpus_ttl_hours: int = Field(default=24, ge=1, le=72)
    style_distillation_max_calls: int = Field(default=28, ge=3, le=64)
    style_distillation_chunk_bytes: int = Field(default=24_000, ge=4000, le=64_000)
    style_distillation_max_input_bytes: int = Field(default=64_000, ge=16_000, le=128_000)
    style_distillation_output_tokens: int = Field(default=1600, ge=600, le=4096)
    style_chat_context_before: int = Field(default=4, ge=0, le=20)
    style_chat_context_after: int = Field(default=2, ge=0, le=20)
    style_chat_max_windows: int = Field(default=24, ge=1, le=64)
    autonomy_enabled: bool = True
    proactive_enabled: bool = True
    shared_daily_usd: Decimal = Field(default=Decimal("1.00"), gt=0)
    autonomous_scene_interval_minutes: int = Field(default=120, ge=1, le=1440)
    autonomous_scene_limit: int = Field(default=2, ge=1, le=10)

    @property
    def embedding_connection(self) -> tuple[str, str]:
        """A separate provider must supply both fields; never send the chat key to it."""
        key = self.embedding_api_key.get_secret_value()
        if self.embedding_base_url or key:
            return self.embedding_base_url.rstrip("/"), key
        return self.model_base_url.rstrip("/"), self.model_api_key.get_secret_value()

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
