"""Builds the service graph once. Tests override `get_plan_service`."""

from functools import lru_cache

from backend.clients.nominatim import NominatimClient
from backend.clients.overpass import OverpassClient
from backend.clients.routing import RoutingClient
from backend.config import get_settings
from backend.repositories.sqlite_repo import SqliteRepo
from backend.services.agent import TripAgent, build_llm
from backend.services.plan_service import PlanService
from backend.support.agent import SupportAgent
from backend.support.retriever import PolicyRetriever
from backend.support.service import SupportService


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
        llm = build_llm(s.llm_provider, s.llm_api_key, s.llm_model, s.llm_timeout_s)
        agent = TripAgent(llm, overpass, nominatim, router)
    return PlanService(
        repo=get_repo(),
        nominatim=nominatim,
        overpass=overpass,
        router=router,
        agent=agent,
    )


@lru_cache
def get_support_service() -> SupportService:
    s = get_settings()
    retriever = PolicyRetriever(s.rag_data_path, s.chroma_path, s.embedding_model,
                                s.rag_score_threshold, s.rag_top_k)
    agent = None
    if s.agent_enabled:
        llm = build_llm(s.llm_provider, s.llm_api_key, s.llm_model, s.llm_timeout_s, temperature=0.1)
        agent = SupportAgent(llm, s.llm_model)
    return SupportService(get_repo(), retriever, agent, home_country=s.home_country, model_name=s.llm_model,
                          key_name=s.llm_key_name)
