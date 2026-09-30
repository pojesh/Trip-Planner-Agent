"""PlanService: runs the agent, then routes, times, validates and saves the plan.

`generate` and `chat` are generators yielding progress events (dicts) so the
UI can show real progress; the router streams them as NDJSON.
"""

import json
import logging
from datetime import timedelta
from typing import Iterator, Optional

from backend.clients.nominatim import NominatimClient
from backend.clients.overpass import OverpassClient
from backend.clients.routing import RoutingClient
from backend.domain.geo import radius_for_bbox
from backend.domain.models import (
    PACE_STOPS, AddStopRequest, ChatMessage, Day, GeocodeRequest, ItineraryPlan, Location,
    PlaceResult, Poi, StopUpdate, Trip, TripConstraints, TripRequest, TripStatus, TripSummary, utc_now,
)
from backend.errors import AppError, not_found
from backend.repositories.sqlite_repo import SqliteRepo
from backend.services import scheduling as sch
from backend.services.agent import AgentContext, TripAgent, candidate_rows, discover
from backend.services.validator import trip_level_issues, validate_day

log = logging.getLogger(__name__)

MAX_QUESTION_ROUNDS = 2
CHAT_HISTORY = 10


def stage(key: str, label: str) -> dict:
    return {"type": "stage", "stage": key, "label": label}


class PlanService:
    def __init__(self, repo: SqliteRepo, nominatim: NominatimClient, overpass: OverpassClient,
                 router: RoutingClient, agent: Optional[TripAgent]):
        self.repo = repo
        self.nominatim = nominatim
        self.overpass = overpass
        self.router = router
        self.agent = agent  # None when GEMINI_API_KEY is not set

    def geocode(self, req: GeocodeRequest) -> list[PlaceResult]:
        return self.nominatim.search_place(req.query, req.limit, req.near_bbox)

    # ================================================================ generate

    def generate(self, req: TripRequest) -> Iterator[dict]:
        dest = req.destination
        ctx = AgentContext(
            destination=Location(label=dest.label, lat=dest.lat, lon=dest.lon),
            radius_m=radius_for_bbox(dest.boundingbox), origin=req.origin, mode=req.transport_mode,
            daily_start=req.daily_start_time, daily_end=req.daily_end_time,
            return_to_start=req.return_to_start, day_count=req.day_count,
        )
        plan: Optional[ItineraryPlan] = None
        planner = "gemini"

        if self.agent:
            yield stage("think", "The agent is planning your trip…")
            can_ask = req.allow_questions and len(req.clarifications) < MAX_QUESTION_ROUNDS
            try:
                result = yield from self.agent.run(ctx, self._task(req), allow_questions=can_ask)
            except AppError as e:
                log.warning("agent failed (%s); using the built-in planner", e.code)
                result = None
            if result and result.kind == "plan":
                plan = result.plan
            elif result and (result.kind == "question" or ctx.candidates):
                # ask_user (or a plain-text reply once places were found) goes back to the traveller.
                # A text reply with no places found means the search failed: use the built-in planner.
                yield {"type": "question", "question": result.message, "quick_replies": result.quick_replies}
                return

        if plan is None:  # no API key, or the agent failed: deterministic built-in planner
            planner = "built-in"
            yield stage("discover", f"Finding real places around {dest.name}…")
            ctx.candidates, ctx.must_see_missing = {}, []
            discover(self.overpass, self.nominatim, ctx, [i.value for i in req.interests], req.must_see)
            plan = sch.fallback_plan(list(ctx.candidates.values()), req.day_count, req.pace,
                                     (req.origin.lat, req.origin.lon), req.daily_end_time)
            plan.assumptions.insert(0, "Planned by the built-in planner (AI agent not available).")

        if not ctx.candidates:
            raise AppError("no_candidates",
                           f"No matching places found near {dest.name} in OpenStreetMap. "
                           "Try more interests or a larger destination.", http_status=404)

        yield stage("build", "Building your day-by-day itinerary…")
        candidates = list(ctx.candidates.values())
        now = utc_now()
        trip = Trip(
            id=sch.new_id(),
            title=f"{req.day_count} day{'s' if req.day_count > 1 else ''} in {dest.name}",
            status=TripStatus.generated,
            destination=ctx.destination, origin=req.origin, origin_mode=req.origin_mode,
            start_date=req.start_date, end_date=req.end_date,
            daily_start_time=req.daily_start_time, daily_end_time=req.daily_end_time,
            transport_mode=req.transport_mode, pace=req.pace, interests=req.interests,
            constraints=TripConstraints(
                must_see=req.must_see, notes=req.notes, return_to_start=req.return_to_start,
                search_radius_m=ctx.radius_m, destination_bbox=dest.boundingbox, planner=planner,
                must_see_missing=ctx.must_see_missing,
            ),
            candidates=candidates, created_at=now, updated_at=now,
        )
        self._apply_plan(trip, plan)
        for qa in req.clarifications:
            trip.chat += [ChatMessage(role="assistant", content=qa.question),
                          ChatMessage(role="user", content=qa.answer)]
        trip.chat.append(ChatMessage(role="assistant", content=plan.summary))
        self.repo.save_trip(trip)
        yield {"type": "result", "trip": trip.model_dump(mode="json")}

    def _task(self, req: TripRequest) -> str:
        lo, hi, target = PACE_STOPS[req.pace]
        lines = [
            "Plan this trip.",
            f"Destination: {req.destination.label}",
            f"Start point (each day starts{' and ends' if req.return_to_start else ''} here): {req.origin.label}",
            f"Dates: {req.start_date} to {req.end_date} ({req.day_count} day(s))",
            f"Daily time window: {req.daily_start_time}-{req.daily_end_time}",
            f"Transport: {req.transport_mode.value}",
            f"Pace: {req.pace.value} ({lo}-{hi} stops per day, about {target} is ideal)",
            f"Interests: {', '.join(i.value for i in req.interests)}",
        ]
        if req.must_see:
            lines.append(f"Must-see places: {', '.join(req.must_see)}")
        if req.notes.strip():
            lines.append(f"Traveller notes: {req.notes.strip()}")
        for qa in req.clarifications:
            lines.append(f"You asked: {qa.question} — traveller answered: {qa.answer}")
        return "\n".join(lines)

    # ================================================================ chat

    def chat(self, trip_id: str, message: str) -> Iterator[dict]:
        trip = self._load(trip_id)
        if self.agent is None:
            raise AppError("agent_disabled", "The AI assistant needs GEMINI_API_KEY in .env. "
                           "You can still edit the plan by hand.", http_status=503)
        trip.chat.append(ChatMessage(role="user", content=message))
        ctx = AgentContext(
            destination=trip.destination, radius_m=trip.constraints.search_radius_m, origin=trip.origin,
            mode=trip.transport_mode, daily_start=trip.daily_start_time, daily_end=trip.daily_end_time,
            return_to_start=trip.constraints.return_to_start, day_count=len(trip.days),
            candidates={c.id: c for c in trip.candidates},
        )
        yield stage("think", "Thinking about your request…")
        result = yield from self.agent.run(ctx, self._chat_task(trip), allow_questions=True)
        trip.candidates = list(ctx.candidates.values())

        changed = result.kind == "plan"
        if changed:
            yield stage("build", "Updating routes and times…")
            notes = {s.candidate_id: s.user_note for d in trip.days for s in d.stops if s.user_note}
            self._apply_plan(trip, result.plan, keep_notes=notes)
            trip.constraints.planner = "gemini"
        trip.chat.append(ChatMessage(role="assistant", content=result.message or "Done — plan updated.",
                                     quick_replies=result.quick_replies, changed_plan=changed))
        trip.updated_at = utc_now()
        self.repo.save_trip(trip)
        yield {"type": "result", "trip": trip.model_dump(mode="json"), "changed": changed}

    def _chat_task(self, trip: Trip) -> str:
        lines = [
            f"Trip: {trip.destination.label}, {trip.start_date} to {trip.end_date} ({len(trip.days)} day(s)).",
            f"Start point each day: {trip.origin.label}. Window {trip.daily_start_time}-{trip.daily_end_time}, "
            f"{trip.transport_mode.value}, {trip.pace.value} pace, interests: {', '.join(i.value for i in trip.interests)}.",
        ]
        if trip.constraints.notes:
            lines.append(f"Traveller notes: {trip.constraints.notes}")
        lines.append("\nCURRENT PLAN (times from the routing service):")
        for d in trip.days:
            lines.append(f"Day {d.day_number} ({d.trip_date}):")
            for s in d.stops:
                note = f" — note: {s.user_note}" if s.user_note else ""
                fixed = f" (earliest_start {s.earliest_start})" if s.earliest_start else ""
                lines.append(f"  {s.sequence}. [{s.candidate_id}] {s.name} ({s.category}) "
                             f"{s.planned_arrival}-{s.planned_departure}, {s.visit_minutes} min{fixed}{note}")
        lines.append("\nKNOWN CANDIDATES (you can also call find_places for more):")
        lines.append(json.dumps(candidate_rows(trip.candidates, trip.origin), ensure_ascii=False))
        lines.append("\nCONVERSATION (latest last):")
        lines += [f"{m.role}: {m.content}" for m in trip.chat[-CHAT_HISTORY:]]
        lines.append("\nReply to the traveller's last message. To change the plan, call submit_itinerary "
                     "with the COMPLETE plan for every day, keeping stops they didn't ask to change.")
        return "\n".join(lines)

    # ================================================================ building days

    def _apply_plan(self, trip: Trip, plan: ItineraryPlan, keep_notes: Optional[dict[str, str]] = None) -> None:
        """Turn a checked plan into routed, timed, validated days on the trip."""
        by_id = {c.id: c for c in trip.candidates}
        old_days = {d.day_number: d for d in trip.days}
        keep_notes = keep_notes or {}
        trip.days = []
        for ad in sorted(plan.days, key=lambda d: d.day_number):
            stops = [sch.poi_to_stop(by_id[s.candidate_id], s.visit_minutes, s.reason,
                                     keep_notes.get(s.candidate_id), s.earliest_start)
                     for s in ad.stops if s.candidate_id in by_id]
            old = old_days.get(ad.day_number)
            day = Day(
                id=old.id if old else sch.new_id(), day_number=ad.day_number,
                trip_date=trip.start_date + timedelta(days=ad.day_number - 1),
                start_location_label=trip.origin.label, summary=ad.summary,
                stops=stops, legs=old.legs if old else [],  # old legs let unchanged routes be reused
            )
            self._refresh_day(trip, day)
            trip.days.append(day)
        trip.agent_summary = plan.summary
        back = " and returns there." if trip.constraints.return_to_start else "."
        trip.constraints.assumptions = list(dict.fromkeys(
            plan.assumptions + [f"Each day starts at {trip.origin.label}{back}"]))
        self._refresh_trip_warnings(trip)

    def _refresh_day(self, trip: Trip, day: Day, force: bool = False) -> int:
        """Route (reusing unchanged legs), time and validate one day. Returns legs re-routed."""
        sch.renumber(day.stops)
        n = sch.route_day(day, trip.origin, trip.transport_mode, trip.constraints.return_to_start,
                          self.router, force=force)
        sch.schedule_day(day, trip.daily_start_time)
        validate_day(day, pace=trip.pace, mode=trip.transport_mode, daily_start=trip.daily_start_time,
                     daily_end=trip.daily_end_time, return_to_start=trip.constraints.return_to_start)
        return n

    def _refresh_trip_warnings(self, trip: Trip) -> None:
        must = [c for c in trip.candidates if c.must_see]
        trip.constraints.warnings = trip_level_issues(trip.days, must, trip.constraints.must_see_missing)

    # ================================================================ trips + manual edits

    def _load(self, trip_id: str) -> Trip:
        trip = self.repo.get_trip(trip_id)
        if trip is None:
            raise not_found("Trip")
        return trip

    @staticmethod
    def _find_day(trip: Trip, day_id: str) -> Day:
        day = next((d for d in trip.days if d.id == day_id), None)
        if day is None:
            raise not_found("Day")
        return day

    @staticmethod
    def _candidate(trip: Trip, candidate_id: str) -> Poi:
        poi = next((c for c in trip.candidates if c.id == candidate_id), None)
        if poi is None:
            raise AppError("unknown_candidate", "That place isn't among this trip's sourced places.",
                           http_status=404)
        return poi

    def get_trip(self, trip_id: str) -> Trip:
        return self._load(trip_id)

    def list_trips(self) -> list[TripSummary]:
        return self.repo.list_trips()

    def delete_trip(self, trip_id: str) -> None:
        if not self.repo.delete_trip(trip_id):
            raise not_found("Trip")

    def save_trip(self, trip_id: str, title: Optional[str]) -> Trip:
        trip = self._load(trip_id)
        if title and title.strip():
            trip.title = title.strip()
        trip.status = TripStatus.saved
        trip.updated_at = utc_now()
        self.repo.save_trip(trip)
        return trip

    def _commit(self, trip: Trip, day: Day, force: bool = False) -> dict:
        n = self._refresh_day(trip, day, force=force)
        self._refresh_trip_warnings(trip)
        trip.updated_at = utc_now()
        self.repo.save_trip(trip)
        return {"trip": trip.model_dump(mode="json"), "rerouted_legs": n}

    def update_stop(self, trip_id: str, day_id: str, stop_id: str, upd: StopUpdate) -> dict:
        trip = self._load(trip_id)
        day = self._find_day(trip, day_id)
        idx = next((i for i, s in enumerate(day.stops) if s.id == stop_id), None)
        if idx is None:
            raise not_found("Stop")
        stop = day.stops[idx]
        if upd.note is not None:
            stop.user_note = upd.note.strip() or None
        if upd.visit_minutes is not None:
            stop.visit_minutes = upd.visit_minutes
        if upd.replace_with:
            poi = self._candidate(trip, upd.replace_with)
            if any(s.candidate_id == poi.id for s in day.stops):
                raise AppError("already_in_day", f"{poi.name} is already in this day.")
            day.stops[idx] = sch.poi_to_stop(poi, sch.default_visit_minutes(poi.category),
                                             "Chosen by you", stop.user_note)
        if upd.move_to is not None:
            moved = day.stops.pop(idx)
            day.stops.insert(min(upd.move_to, len(day.stops) + 1) - 1, moved)
        return self._commit(trip, day)

    def remove_stop(self, trip_id: str, day_id: str, stop_id: str) -> dict:
        trip = self._load(trip_id)
        day = self._find_day(trip, day_id)
        if not any(s.id == stop_id for s in day.stops):
            raise not_found("Stop")
        day.stops = [s for s in day.stops if s.id != stop_id]
        return self._commit(trip, day)

    def add_stop(self, trip_id: str, day_id: str, req: AddStopRequest) -> dict:
        trip = self._load(trip_id)
        day = self._find_day(trip, day_id)
        poi = self._candidate(trip, req.candidate_id)
        if any(s.candidate_id == poi.id for s in day.stops):
            raise AppError("already_in_day", f"{poi.name} is already in this day.")
        pos = sch.best_insert_position(day.stops, (trip.origin.lat, trip.origin.lon), poi.lat, poi.lon)
        day.stops.insert(pos, sch.poi_to_stop(poi, sch.default_visit_minutes(poi.category), "Added by you"))
        return self._commit(trip, day)

    def recalculate(self, trip_id: str, day_id: str) -> dict:
        trip = self._load(trip_id)
        return self._commit(trip, self._find_day(trip, day_id), force=True)
