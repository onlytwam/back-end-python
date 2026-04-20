"""
config.py
──────────
Reads values from environment variables only.
  Local dev  → .env file
  Production → Azure App Settings (overrides .env completely)

Priority: Azure App Settings > .env file > config.py default (last resort)

Company info, SMS, OAuth secrets, payment keys — all come from
the AppSettings DB table via get_setting(). NOT here.
"""

from typing import List
from pydantic import field_validator, ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Required — app refuses to start if any of these are missing ───────────
    database_url: str   # local: postgres://localhost  |  prod: Azure PostgreSQL
    secret_key:   str   # same value in both environments
    cors_origins: str   # local: localhost origins     |  prod: Azure + twam.in origins
    jwt_issuer:   str   # local: http://127.0.0.1:8000 |  prod: https://twam-backend...azurecontainerapps.io

    # ── Safe defaults — same value everywhere, never needs to change ──────────
    jwt_algorithm:       str = "HS512"
    jwt_audience:        str = "twam-web-portal"
    jwt_expires_minutes: int = 525600      # 1 year

    # ── Optional with defaults ────────────────────────────────────────────────
    base_url: str = ""          # local: http://127.0.0.1:8000 | prod: Azure Blob URL
    app_env:  str = "development"

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("secret_key")
    @classmethod
    def secret_key_must_exist(cls, v: str) -> str:
        if not v or len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters.")
        return v

    @field_validator("database_url")
    @classmethod
    def database_url_must_exist(cls, v: str) -> str:
        if not v:
            raise ValueError("DATABASE_URL must be set.")
        return v

    @field_validator("jwt_issuer")
    @classmethod
    def jwt_issuer_must_exist(cls, v: str) -> str:
        if not v:
            raise ValueError("JWT_ISSUER must be set — local: http://127.0.0.1:8000, prod: your Azure backend URL.")
        return v

    # ── Computed properties ───────────────────────────────────────────────────
    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


settings = Settings()