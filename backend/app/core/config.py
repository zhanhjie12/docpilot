"""Environment-backed configuration for the Web backend."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    """Small, non-secret configuration surface for the MVP."""

    app_env: str = "dev"
    database_url: str = "sqlite:///./data/docpilot.db"
    upload_dir: Path = Path("./data/uploads")
    task_backend: str = "inline"
    llm_provider: str = "mock"
    llm_api_key: str = ""
    # LLM_PROVIDER=mock keeps the local MVP independent of a model service.
    # For a third-party OpenAI-compatible relay, set all three LLM_* values.
    llm_base_url: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 60.0
    embedding_provider: str = "disabled"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_device: str = "cpu"
    embedding_batch_size: int = 16
    embedding_timeout_seconds: float = 60.0
    embedding_min_score: float = 0.25
    rerank_model: str = ""
    jwt_secret: str = "change-me-in-development"
    # 开发环境默认不强制业务接口登录；部署环境可通过 AUTH_REQUIRED=true 开启。
    auth_required: bool = False
    access_token_expire_minutes: int = 60
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_upload_bytes: int = 25 * 1024 * 1024
    task_workers: int = 2

    model_config = SettingsConfigDict(
        # 环境变量优先；backend/.env 可覆盖项目根目录的 .env。
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_llm_configuration(self) -> "Settings":
        """Fail early when a real provider is selected without its API settings.

        The provider adapter uses the OpenAI-compatible contract: an API key,
        an HTTP(S) base URL (usually ending in /v1), and a model name.
        mock remains valid without those values so local tests do not need
        network access or secrets.
        """

        self.llm_provider = self.llm_provider.strip().lower()
        if self.llm_provider in {"openai", "openai-compatible"}:
            self.llm_provider = "openai_compatible"
        if self.llm_provider not in {"mock", "openai_compatible"}:
            raise ValueError("LLM_PROVIDER must be mock or openai_compatible")
        self.llm_api_key = self.llm_api_key.strip()
        self.llm_base_url = self.llm_base_url.strip().rstrip("/")
        self.llm_model = self.llm_model.strip()

        if self.llm_timeout_seconds <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be greater than 0")
        if self.access_token_expire_minutes <= 0:
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES must be greater than 0")
        if self.auth_required and len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters when AUTH_REQUIRED=true")

        if self.llm_provider == "mock":
            return self

        missing = [
            name
            for name, value in (
                ("LLM_API_KEY", self.llm_api_key),
                ("LLM_BASE_URL", self.llm_base_url),
                ("LLM_MODEL", self.llm_model),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"LLM_PROVIDER={self.llm_provider!r} requires: {', '.join(missing)}"
            )

        if not self.llm_base_url.startswith(("http://", "https://")):
            raise ValueError("LLM_BASE_URL must start with http:// or https://")

        return self

    @model_validator(mode="after")
    def validate_embedding_configuration(self) -> "Settings":
        """Validate local SentenceTransformer or OpenAI-compatible settings."""

        aliases = {
            "none": "disabled",
            "off": "disabled",
            "sentence-transformer": "local",
            "sentence-transformers": "local",
            "sentence_transformers": "local",
            "openai": "openai_compatible",
            "openai-compatible": "openai_compatible",
        }
        provider = self.embedding_provider.strip().lower()
        self.embedding_provider = aliases.get(provider, provider)
        self.embedding_model = self.embedding_model.strip()
        self.embedding_api_key = self.embedding_api_key.strip()
        self.embedding_base_url = self.embedding_base_url.strip().rstrip("/")
        self.embedding_device = self.embedding_device.strip() or "cpu"

        if self.embedding_provider not in {"disabled", "local", "openai_compatible"}:
            raise ValueError(
                "EMBEDDING_PROVIDER must be disabled, local, or openai_compatible"
            )
        if self.embedding_batch_size <= 0:
            raise ValueError("EMBEDDING_BATCH_SIZE must be greater than 0")
        if self.embedding_timeout_seconds <= 0:
            raise ValueError("EMBEDDING_TIMEOUT_SECONDS must be greater than 0")
        if not -1.0 <= self.embedding_min_score <= 1.0:
            raise ValueError("EMBEDDING_MIN_SCORE must be between -1 and 1")
        if self.embedding_provider == "disabled":
            return self
        if not self.embedding_model:
            raise ValueError("EMBEDDING_MODEL is required when embeddings are enabled")

        if self.embedding_provider == "openai_compatible":
            missing = []
            if not self.effective_embedding_api_key:
                missing.append("EMBEDDING_API_KEY or LLM_API_KEY")
            if not self.effective_embedding_base_url:
                missing.append("EMBEDDING_BASE_URL or LLM_BASE_URL")
            if missing:
                raise ValueError(
                    "EMBEDDING_PROVIDER='openai_compatible' requires: "
                    + ", ".join(missing)
                )
            if not self.effective_embedding_base_url.startswith(("http://", "https://")):
                raise ValueError(
                    "EMBEDDING_BASE_URL (or LLM_BASE_URL) must start with http:// or https://"
                )
        return self

    @property
    def effective_embedding_api_key(self) -> str:
        """Use an embedding-specific key, or reuse the relay LLM key."""

        return self.embedding_api_key or self.llm_api_key

    @property
    def effective_embedding_base_url(self) -> str:
        """Use an embedding-specific endpoint, or reuse the relay base URL."""

        return self.embedding_base_url or self.llm_base_url

    @property
    def cors_origin_list(self) -> list[str]:
        """Parse comma-separated origins without requiring JSON in ``.env``."""

        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings object per process."""

    return Settings()
