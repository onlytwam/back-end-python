"""
config.py
──────────
Reads ALL values from environment variables.
  Local dev  → .env file
  Production → Azure App Settings

No hardcoded defaults. If a value is missing the app fails fast on startup.
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

    # Values come entirely from .env or Azure App Settings
    database_url:        str
    secret_key:          str
    jwt_algorithm:       str
    jwt_issuer:          str
    jwt_audience:        str
    jwt_expires_minutes: int
    cors_origins:        str
    base_url:            str
    app_env:             str

    @field_validator("secret_key")
    @classmethod
    def secret_key_must_exist(cls, v: str) -> str:
        if not v or len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters.")
        return v

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


settings = Settings()