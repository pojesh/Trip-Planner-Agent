"""Backend configuration, loaded from environment variables / .env."""

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_USER_AGENT = "Wayfinder-TripPlanner/1.0 (github.com/POJESH/Trip-Planner-Agent)"
LLM_PROVIDERS = ("gemini", "groq")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    # LLM for both agents: "gemini" (Google) or "groq" (Groq Cloud, e.g. openai/gpt-oss-120b)
    llm_provider: str = "gemini"
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-2.5-flash"
    groq_api_key: SecretStr = SecretStr("")
    groq_model: str = "openai/gpt-oss-120b"

    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    overpass_base_url: str = "https://overpass-api.de/api/interpreter"
    overpass_fallback_url: str = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    routing_base_url: str = "https://routing.openstreetmap.de"
    http_user_agent: str = DEFAULT_USER_AGENT

    sqlite_path: str = "data/trip_planner.db"

    nominatim_timeout_s: float = 6
    overpass_timeout_s: float = 25
    routing_timeout_s: float = 8
    llm_timeout_s: float = Field(default=45, validation_alias=AliasChoices("llm_timeout_s", "gemini_timeout_s"))

    # Support assistant (RAG over the policy PDFs in rag/data)
    home_country: str = "India"  # destinations here are "domestic", everything else "international"
    rag_data_dir: str = "rag/data"
    chroma_dir: str = "rag/chroma"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    rag_score_threshold: float = 0.5  # cosine relevance below this counts as "no relevant policy"
    rag_top_k: int = 5

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

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in LLM_PROVIDERS:
            raise ValueError(f"LLM_PROVIDER must be one of {', '.join(LLM_PROVIDERS)} (got '{v}')")
        return v

    @property
    def llm_api_key(self) -> str:
        key = self.groq_api_key if self.llm_provider == "groq" else self.gemini_api_key
        return key.get_secret_value().strip()

    @property
    def llm_model(self) -> str:
        return self.groq_model if self.llm_provider == "groq" else self.gemini_model

    @property
    def llm_key_name(self) -> str:
        return f"{self.llm_provider.upper()}_API_KEY"

    @property
    def agent_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def db_path(self) -> Path:
        return _abs(self.sqlite_path)

    @property
    def rag_data_path(self) -> Path:
        return _abs(self.rag_data_dir)

    @property
    def chroma_path(self) -> Path:
        return _abs(self.chroma_dir)


def _abs(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT_DIR / p


@lru_cache
def get_settings() -> Settings:
    return Settings()


def check_settings(settings: Settings) -> None:
    """Log actionable startup warnings. Never logs secret values."""
    if not settings.agent_enabled:
        log.warning(
            "%s is not set (LLM_PROVIDER=%s). Trips will be planned with the built-in "
            "deterministic planner, the planner chat is disabled and the support assistant "
            "only shows matching policy sections. Add %s=<your key> to .env to enable the AI agents.",
            settings.llm_key_name, settings.llm_provider, settings.llm_key_name,
        )
