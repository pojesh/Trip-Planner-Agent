"""Backend configuration, loaded from environment variables / .env."""

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_USER_AGENT = "Wayfinder-TripPlanner/1.0 (github.com/POJESH/Trip-Planner-Agent)"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-2.5-flash"

    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    overpass_base_url: str = "https://overpass-api.de/api/interpreter"
    overpass_fallback_url: str = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    routing_base_url: str = "https://routing.openstreetmap.de"
    http_user_agent: str = DEFAULT_USER_AGENT

    sqlite_path: str = "data/trip_planner.db"

    nominatim_timeout_s: float = 6
    overpass_timeout_s: float = 25
    routing_timeout_s: float = 8
    gemini_timeout_s: float = 45

    @field_validator("nominatim_base_url", "overpass_base_url", "overpass_fallback_url", "routing_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            raise ValueError(f"'{v}' must start with http:// or https://")
        return v

    @field_validator("http_user_agent")
    @classmethod
    def _user_agent(cls, v: str) -> str:
        v = v.strip()
        if not v or "example.com" in v or "example.org" in v:
            # Nominatim answers 403 to empty/placeholder User-Agents; use a real identifying one.
            log.warning("HTTP_USER_AGENT is empty or a placeholder; using '%s'", DEFAULT_USER_AGENT)
            return DEFAULT_USER_AGENT
        return v

    @property
    def agent_enabled(self) -> bool:
        return bool(self.gemini_api_key.get_secret_value().strip())

    @property
    def db_path(self) -> Path:
        p = Path(self.sqlite_path)
        return p if p.is_absolute() else ROOT_DIR / p


@lru_cache
def get_settings() -> Settings:
    return Settings()


def check_settings(settings: Settings) -> None:
    """Log actionable startup warnings. Never logs secret values."""
    if not settings.agent_enabled:
        log.warning(
            "GEMINI_API_KEY is not set. Trips will be planned with the built-in "
            "deterministic planner and the chat assistant is disabled. "
            "Add GEMINI_API_KEY=<your key> to .env to enable the AI agent."
        )
