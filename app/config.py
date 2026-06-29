"""Application settings, loaded from environment variables (Section 15 of the spec).

Single source of truth for configuration. Anything secret comes from the
environment — never hard-coded, never committed.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_name: str = "AI Gita Mentor"
    app_base_url: str = Field(default="http://localhost:8000", alias="APP_BASE_URL")
    environment: str = Field(default="development", alias="ENVIRONMENT")
    debug: bool = Field(default=False, alias="DEBUG")

    # Comma-separated emails auto-granted the admin role on signup/login.
    admin_emails: str = Field(default="", alias="ADMIN_EMAILS")

    # Where in-browser recordings land temporarily before Drive copy (Section 6).
    recordings_tmp_dir: str = Field(default="recordings_tmp", alias="RECORDINGS_TMP_DIR")

    # --- Database ---
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    # --- LLM / embeddings / baselines ---
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    perplexity_api_key: str | None = Field(default=None, alias="PERPLEXITY_API_KEY")

    # Model defaults (overridable per-version via the ai_config table).
    chat_model: str = Field(default="claude-opus-4-8", alias="CHAT_MODEL")
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(default=1536, alias="EMBEDDING_DIM")
    transcribe_model: str = Field(default="gpt-4o-mini-transcribe", alias="TRANSCRIBE_MODEL")

    # --- Auth ---
    jwt_secret: str = Field(default="dev-insecure-change-me", alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_expire_minutes: int = Field(default=60 * 24 * 14, alias="JWT_EXPIRE_MINUTES")
    google_oauth_client_id: str | None = Field(default=None, alias="GOOGLE_OAUTH_CLIENT_ID")
    google_oauth_client_secret: str | None = Field(default=None, alias="GOOGLE_OAUTH_CLIENT_SECRET")
    google_oauth_redirect_uri: str | None = Field(default=None, alias="GOOGLE_OAUTH_REDIRECT_URI")

    # --- Google Drive (audio archive) ---
    google_drive_client_id: str | None = Field(default=None, alias="GOOGLE_DRIVE_CLIENT_ID")
    google_drive_client_secret: str | None = Field(default=None, alias="GOOGLE_DRIVE_CLIENT_SECRET")
    google_drive_refresh_token: str | None = Field(default=None, alias="GOOGLE_DRIVE_REFRESH_TOKEN")
    drive_recordings_folder_id: str | None = Field(default=None, alias="DRIVE_RECORDINGS_FOLDER_ID")

    # --- Payments (Razorpay) ---
    razorpay_key_id: str | None = Field(default=None, alias="RAZORPAY_KEY_ID")
    razorpay_key_secret: str | None = Field(default=None, alias="RAZORPAY_KEY_SECRET")
    razorpay_webhook_secret: str | None = Field(default=None, alias="RAZORPAY_WEBHOOK_SECRET")

    # --- Analytics (Meta) ---
    meta_pixel_id: str | None = Field(default=None, alias="META_PIXEL_ID")
    meta_capi_token: str | None = Field(default=None, alias="META_CAPI_TOKEN")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
