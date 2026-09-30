"""Builds the service graph once. Tests override `get_plan_service`."""

from functools import lru_cache

from backend.clients.nominatim import NominatimClient
from backend.clients.overpass import OverpassClient
from backend.clients.routing import RoutingClient
from backend.config import get_settings
from backend.repositories.sqlite_repo import SqliteRepo
from backend.services.agent import TripAgent, build_llm
from backend.services.plan_service import PlanService


@lru_cache
def get_repo() -> SqliteRepo:
    return SqliteRepo(get_settings().db_path)


@lru_cache
def get_plan_service() -> PlanService:
    s = get_settings()
    overpass = OverpassClient(s.overpass_base_url, s.overpass_fallback_url, s.http_user_agent,
                              s.overpass_timeout_s)
    router = RoutingClient(s.routing_base_url, s.http_user_agent, s.routing_timeout_s)
    nominatim = NominatimClient(s.nominatim_base_url, s.http_user_agent, s.nominatim_timeout_s)
    agent = None
    if s.agent_enabled:
        llm = build_llm(s.gemini_api_key.get_secret_value(), s.gemini_model, s.gemini_timeout_s)
        agent = TripAgent(llm, overpass, nominatim, router)
    return PlanService(
        repo=get_repo(),
        nominatim=nominatim,
        overpass=overpass,
        router=router,
        agent=agent,
    )
