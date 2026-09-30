"""LangChain tool-calling agent that plans (and re-plans) the itinerary.

The loop is deliberately explicit so it is easy to follow:

    messages = [system, task]
    repeat:
        ai = llm_with_tools.invoke(messages)
        no tool call          -> plain reply to the traveller
        find_places/check_route -> run it, append ToolMessage, continue
        ask_user              -> stop and return the question
        submit_itinerary      -> validate; valid -> return plan, else send errors back

The model only ever picks from places returned by `find_places` (real OSM data);
routes and times come from `check_route` (OSRM). It cannot invent either.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, ValidationError

from backend.clients.nominatim import NominatimClient
from backend.clients.overpass import OverpassClient
from backend.clients.routing import RoutingClient
from backend.domain.geo import DAY_TRIP_RADIUS_M, bbox_around, haversine_m, radius_for_bbox
from backend.domain.models import (
    AgentStop, Day, Interest, ItineraryPlan, Location, Poi, TransportMode, fmt_hhmm, parse_hhmm,
)
from backend.errors import AppError
from backend.services import scheduling as sch
from backend.services.validator import day_end_minutes

log = logging.getLogger(__name__)

MAX_STEPS = 10

SYSTEM_PROMPT = """You are a travel-planning coordinator inside a trip planner app.

How to work:
1. Call find_places to get real candidate places (from OpenStreetMap) for the traveller's interests
   and must-see places. You may call it again later for other kinds of places.
   Read the traveller's notes and messages carefully: if they name a specific place, look it up with
   must_see; if they want a day trip to a nearby town (e.g. "a day in Mahabalipuram"), call find_places
   with near_place set to that town and plan that whole day there (check_route shows the travel time).
2. Choose places ONLY from candidates returned by find_places, referring to them by their exact "id".
3. Keep each day in a compact area and use the traveller's whole daily window.
   For walking trips, choose places close to each other (ideally under ~2 km apart) and to the start
   point; plan fewer stops rather than long walks, and mention it if sights are far from the start.
   Stops run back-to-back unless you set "earliest_start": use it for things tied to a time of day,
   e.g. lunch 12:30, dinner 19:00-20:00, a sunset viewpoint or an evening beach walk.
   Never call something "dinner" or "evening" unless its earliest_start makes it happen then.
4. Call check_route for each day to see real travel times and the resulting schedule.
   Adjust (fewer stops, shorter visits, closer places) if a day runs past the window.
5. Finish by calling submit_itinerary with every day of the trip, stops in visiting order.

Rules:
- Never invent places, opening hours, prices, bookings or travel times.
- Never state or assume that a place is open; hours are unverified (the app shows this).
  Assumptions must only be about the traveller's preferences, not about opening times or traffic.
- Visit durations must be realistic (10-240 minutes).
- Only call ask_user when the answer would materially change the plan and you cannot make a
  sensible assumption. Otherwise assume, and mention the assumption in submit_itinerary.
- When the traveller only asks a question about the plan, answer in plain text without tools.
- The plan only changes when you call submit_itinerary. Never say you updated, added or changed
  anything unless you called submit_itinerary; if you cannot do what was asked, say so plainly and why.
- Be brief and friendly. No markdown."""


# ------------------------------------------------------------------ tool argument schemas

class FindPlacesArgs(BaseModel):
    interests: list[str] = Field(description="Kinds of places: " + ", ".join(i.value for i in Interest))
    must_see: list[str] = Field(default_factory=list,
                                description="Specific place names to look up, up to ~100 km away (optional)")
    near_place: Optional[str] = Field(
        default=None, description="Optional town/area to search around instead of the city centre, "
                                  "for a day trip (e.g. 'Mahabalipuram'). Must be within ~100 km.")


class CheckRouteArgs(BaseModel):
    stops: list[AgentStop] = Field(description="One day's stops in visiting order")


class AskUserArgs(BaseModel):
    question: str = Field(description="One short question for the traveller")
    quick_replies: list[str] = Field(default_factory=list, description="2-4 short suggested answers")


# ------------------------------------------------------------------ results

@dataclass
class AgentContext:
    """Everything the tools need for one run."""
    destination: Location
    radius_m: int
    origin: Location
    mode: TransportMode
    daily_start: str
    daily_end: str
    return_to_start: bool
    day_count: int
    candidates: dict[str, Poi] = field(default_factory=dict)
    must_see_missing: list[str] = field(default_factory=list)


@dataclass
class AgentResult:
    kind: str  # "plan" | "question" | "reply"
    message: str = ""
    quick_replies: list[str] = field(default_factory=list)
    plan: Optional[ItineraryPlan] = None


def candidate_rows(pois: list[Poi], start: Location) -> list[dict]:
    rows = []
    for p in pois:
        row = {"id": p.id, "name": p.name, "category": p.category,
               "km_from_start": round(haversine_m(start.lat, start.lon, p.lat, p.lon) / 1000, 1)}
        if p.must_see:
            row["must_see"] = True
        if p.opening_hours:
            row["listed_hours"] = p.opening_hours[:60]
        if p.tags.get("cuisine"):
            row["cuisine"] = p.tags["cuisine"][:40]
        rows.append(row)
    return rows


def check_plan(plan: ItineraryPlan, known_ids: set[str], day_count: int) -> list[str]:
    """Deterministic checks on a submitted plan. Returns problems for the agent to fix."""
    errors = []
    numbers = sorted(d.day_number for d in plan.days)
    if numbers != list(range(1, day_count + 1)):
        errors.append(f"Submit exactly {day_count} day(s) numbered 1..{day_count} (got {numbers}).")
    used: set[str] = set()
    for d in plan.days:
        if not d.stops:
            errors.append(f"Day {d.day_number} has no stops.")
        for s in d.stops:
            if s.candidate_id not in known_ids:
                errors.append(f"Day {d.day_number}: '{s.candidate_id}' is not a candidate id from find_places.")
            elif s.candidate_id in used:
                errors.append(f"'{s.candidate_id}' is used more than once in the trip.")
            used.add(s.candidate_id)
            if not 10 <= s.visit_minutes <= 240:
                errors.append(f"Visit time for '{s.candidate_id}' must be 10-240 minutes.")
    return errors


def _redact(text: str) -> str:
    """Strip anything that looks like an API key before logging."""
    return re.sub(r"(key=|AIza|AQ\.)[\w.\-]+", r"\1<redacted>", text)


def _text(ai: AIMessage) -> str:
    """AIMessage content can be a string or a list of parts."""
    if isinstance(ai.content, str):
        return ai.content.strip()
    return " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in ai.content).strip()


def discover(overpass: OverpassClient, nominatim: NominatimClient, ctx: AgentContext,
             interests: list[str], must_see: list[str], near_place: Optional[str] = None) -> list[Poi]:
    """Find real places for interests (Overpass) and named must-sees (Nominatim).

    Normally searches the city circle. With `near_place` (e.g. a day-trip town) it searches
    around that town instead, if the town is within DAY_TRIP_RADIUS_M of the destination.
    Must-see names are looked up within the same day-trip range.
    Adds everything found to the run's candidate pool and returns the newly found places.
    """
    region = bbox_around(ctx.destination.lat, ctx.destination.lon, DAY_TRIP_RADIUS_M)
    lat, lon, radius = ctx.destination.lat, ctx.destination.lon, ctx.radius_m
    if near_place:
        towns = nominatim.search_place(near_place, limit=1, near_bbox=region, only_nearby=True)
        if not towns:
            ctx.must_see_missing = list(dict.fromkeys(ctx.must_see_missing + [near_place]))
            return []
        lat, lon = towns[0].lat, towns[0].lon
        radius = radius_for_bbox(towns[0].boundingbox, 3_000, 6_000)

    valid = [i for i in interests if i in {x.value for x in Interest}]
    pois = overpass.find_pois(lat, lon, radius, valid) if valid else []
    must: list[Poi] = []
    if must_see:
        must, missing = nominatim.find_must_see(must_see, region)
        ctx.must_see_missing = list(dict.fromkeys(ctx.must_see_missing + missing))
    found = list({p.id: p for p in pois + must}.values())  # must-see wins on duplicates
    for p in found:
        if p.must_see or p.id not in ctx.candidates:
            ctx.candidates[p.id] = p
    return found


class TripAgent:
    def __init__(self, llm, overpass: OverpassClient, nominatim: NominatimClient, router: RoutingClient):
        self.llm = llm  # any LangChain chat model that supports bind_tools
        self.overpass = overpass
        self.nominatim = nominatim
        self.router = router

    # -------------------------------------------------------------- tools

    def _tools(self, ctx: AgentContext, allow_questions: bool) -> list[StructuredTool]:
        def find_places(interests: list[str], must_see: Optional[list[str]] = None,
                        near_place: Optional[str] = None) -> str:
            found = discover(self.overpass, self.nominatim, ctx, interests, must_see or [], near_place)
            result = {"candidates": candidate_rows(found, ctx.origin)}
            if near_place:
                result["searched_around"] = near_place
            missing = [m for m in (must_see or []) + ([near_place] if near_place else [])
                       if m in ctx.must_see_missing]
            if missing:
                result["not_found"] = missing
            return json.dumps(result, ensure_ascii=False)

        def check_route(stops: list[AgentStop]) -> str:
            stops = [AgentStop.model_validate(s) if isinstance(s, dict) else s for s in stops]
            known = [s for s in stops if s.candidate_id in ctx.candidates]
            day = Day(id="check", day_number=0, trip_date="2000-01-01", stops=[
                sch.poi_to_stop(ctx.candidates[s.candidate_id], s.visit_minutes,
                                earliest_start=s.earliest_start) for s in known])
            sch.renumber(day.stops)
            sch.route_day(day, ctx.origin, ctx.mode, ctx.return_to_start, self.router)
            sch.schedule_day(day, ctx.daily_start)
            legs = []
            for leg in day.legs:
                to = day.stops[leg.sequence - 1].name if leg.sequence <= len(day.stops) else "start point"
                if leg.duration_seconds is None:
                    legs.append(f"to {to}: no route available (time unknown)")
                else:
                    legs.append(f"to {to}: {leg.duration_seconds / 60:.0f} min, {leg.distance_meters / 1000:.1f} km")
            end = day_end_minutes(day, ctx.return_to_start)
            fits = end is not None and end <= parse_hhmm(ctx.daily_end)
            return json.dumps({
                "legs": legs,
                "schedule": [f"{s.name}: {s.planned_arrival}-{s.planned_departure}" for s in day.stops],
                "day_ends_at": fmt_hhmm(end) if end is not None else None,
                "window": f"{ctx.daily_start}-{ctx.daily_end}",
                "fits_window": fits,
                "unknown_ids": [s.candidate_id for s in stops if s.candidate_id not in ctx.candidates],
            })

        def noop(**_) -> str:  # ask_user / submit_itinerary are handled by the loop
            return "ok"

        tools = [
            StructuredTool.from_function(find_places, name="find_places", args_schema=FindPlacesArgs,
                                         description="Search OpenStreetMap for real candidate places near the destination."),
            StructuredTool.from_function(check_route, name="check_route", args_schema=CheckRouteArgs,
                                         description="Get real travel times and the resulting schedule for one day's ordered stops."),
            StructuredTool.from_function(noop, name="submit_itinerary", args_schema=ItineraryPlan,
                                         description="Submit the final itinerary for all days."),
        ]
        if allow_questions:
            tools.append(StructuredTool.from_function(
                noop, name="ask_user", args_schema=AskUserArgs,
                description="Ask the traveller one follow-up question and wait for the answer."))
        return tools

    # -------------------------------------------------------------- loop

    def run(self, ctx: AgentContext, task: str, allow_questions: bool) -> Iterator[dict]:
        """Generator: yields progress events; returns an AgentResult (use `yield from`)."""
        tools = self._tools(ctx, allow_questions)
        by_name = {t.name: t for t in tools}
        llm = self.llm.bind_tools(tools)
        messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(task)]
        routes_checked = 0

        for _ in range(MAX_STEPS):
            try:
                ai: AIMessage = llm.invoke(messages)
            except Exception as e:  # never leak provider details or keys to the user
                detail = _redact(str(e))
                log.warning("LLM call failed: %s: %s", type(e).__name__, detail[:300])
                busy = any(s in detail for s in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "high demand"))
                raise AppError("agent_busy" if busy else "agent_unavailable",
                               "Gemini is overloaded right now (high demand). Please try again in a minute."
                               if busy else "The AI agent is unavailable right now. Please retry.",
                               retryable=True, provider="Gemini", http_status=503)
            messages.append(ai)

            if not ai.tool_calls:
                return AgentResult(kind="reply", message=_text(ai) or "Sorry, I didn't catch that.")

            for call in ai.tool_calls:
                name, args = call["name"], call["args"]
                if name == "ask_user":
                    q = AskUserArgs.model_validate(args)
                    return AgentResult(kind="question", message=q.question, quick_replies=q.quick_replies[:4])

                if name == "submit_itinerary":
                    try:
                        plan = ItineraryPlan.model_validate(args)
                        errors = check_plan(plan, set(ctx.candidates), ctx.day_count)
                    except ValidationError as e:
                        errors = [f"Invalid arguments: {e.error_count()} problem(s)."]
                    if not errors:
                        return AgentResult(kind="plan", message=plan.summary, plan=plan)
                    output = "Not accepted. Fix these problems and submit again:\n- " + "\n- ".join(errors)
                elif name in by_name:
                    if name == "find_places":
                        yield {"type": "stage", "stage": "discover",
                               "label": "Searching OpenStreetMap for " + ", ".join(args.get("interests", [])) + "…"}
                    else:
                        routes_checked += 1
                        yield {"type": "stage", "stage": "route",
                               "label": f"Checking real travel times (route {routes_checked})…"}
                    try:
                        output = by_name[name].invoke(args)
                    except AppError as e:
                        output = f"Tool failed: {e.message}"
                    except ValidationError:
                        output = "Tool failed: invalid arguments."
                else:
                    output = f"Unknown tool '{name}'."
                messages.append(ToolMessage(content=str(output), tool_call_id=call["id"]))

        raise AppError("agent_gave_up", "The AI agent couldn't finish the plan.", retryable=True,
                       provider="Gemini", http_status=502)


def build_llm(api_key: str, model: str, timeout_s: float):
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(model=model, google_api_key=api_key, temperature=0.3,
                                  timeout=timeout_s, max_retries=5)  
