"""Ordering, routing and timing of a day's stops, plus the deterministic fallback planner."""

import math
import uuid
from typing import Optional

from backend.clients.routing import RoutingClient
from backend.domain.geo import bearing_deg, haversine_m
from backend.domain.models import (
    PACE_STOPS, AgentDay, AgentStop, Day, ItineraryPlan, Leg, Location, Pace, Poi, Stop,
    TransportMode, fmt_hhmm, parse_hhmm,
)

DEFAULT_VISIT_MINUTES = {
    "museum": 90, "gallery": 60, "theatre": 120, "arts centre": 60,
    "castle": 75, "palace": 75, "fort": 60, "ruins": 45, "archaeological site": 60,
    "monument": 20, "memorial": 20, "city gate": 15, "manor": 45, "tower": 30,
    "attraction": 45, "viewpoint": 20,
    "park": 60, "garden": 45, "nature reserve": 90, "beach": 90,
    "restaurant": 75, "cafe": 30, "bar": 60, "pub": 60, "biergarten": 60,
    "marketplace": 45, "mall": 60, "department store": 45,
    "place of worship": 30,
}


def default_visit_minutes(category: Optional[str]) -> int:
    return DEFAULT_VISIT_MINUTES.get((category or "").lower(), 45)


def new_id() -> str:
    return uuid.uuid4().hex


def poi_to_stop(poi: Poi, visit_minutes: int, reason: str = "", note: Optional[str] = None,
                earliest_start: Optional[str] = None) -> Stop:
    return Stop(
        id=new_id(), sequence=0, name=poi.name, category=poi.category,
        osm_type=poi.osm_type, osm_id=poi.osm_id, lat=poi.lat, lon=poi.lon, address=poi.address,
        tags=poi.tags, visit_minutes=visit_minutes, reason=reason or None, user_note=note,
        earliest_start=earliest_start, source_url=poi.source_url, source_fetched_at=poi.fetched_at,
    )


def renumber(stops: list[Stop]) -> None:
    for i, s in enumerate(stops, start=1):
        s.sequence = i


# ------------------------------------------------------------------ ordering

def nearest_neighbour(items: list, start: tuple[float, float], key=lambda s: (s.lat, s.lon)) -> list:
    remaining = list(items)
    ordered = []
    here = start
    while remaining:
        nxt = min(remaining, key=lambda s: haversine_m(*here, *key(s)))
        remaining.remove(nxt)
        ordered.append(nxt)
        here = key(nxt)
    return ordered


def best_insert_position(stops: list[Stop], start: tuple[float, float], lat: float, lon: float) -> int:
    """0-based index where inserting (lat, lon) adds the least detour."""
    pts = [start] + [(s.lat, s.lon) for s in stops]
    best_i, best_cost = len(stops), math.inf
    for i in range(len(pts)):
        a = pts[i]
        b = pts[i + 1] if i + 1 < len(pts) else None
        cost = haversine_m(*a, lat, lon) + (haversine_m(lat, lon, *b) - haversine_m(*a, *b) if b else 0)
        if cost < best_cost:
            best_i, best_cost = i, cost
    return best_i


# ------------------------------------------------------------------ routing + timing

def _pt_key(lat: float, lon: float) -> tuple:
    return (round(lat, 5), round(lon, 5))


def route_day(day: Day, origin: Location, mode: TransportMode, return_to_start: bool,
              router: RoutingClient, force: bool = False) -> int:
    """Rebuild day.legs for the current stop order.

    Legs whose endpoints and mode are unchanged are reused as-is; only new
    pairs are routed. Returns the number of legs that were (re)computed.
    """
    start = (origin.lat, origin.lon)
    points = [start] + [(s.lat, s.lon) for s in day.stops]
    if return_to_start and day.stops:
        points.append(start)
    pairs = list(zip(points[:-1], points[1:]))

    old = {} if force else {
        (_pt_key(g.from_lat, g.from_lon), _pt_key(g.to_lat, g.to_lon), g.mode): g for g in day.legs
    }
    legs: list[Optional[Leg]] = []
    missing: list[int] = []
    for i, (a, b) in enumerate(pairs):
        found = old.get((_pt_key(*a), _pt_key(*b), mode))
        legs.append(found.model_copy() if found else None)
        if not found:
            missing.append(i)

    if missing:
        if len(missing) == len(pairs):
            results = router.route(points, mode, use_cache=not force)  # one call for the whole day
        else:
            results = [router.route([pairs[i][0], pairs[i][1]], mode, use_cache=not force)[0]
                       for i in missing]
        for idx, r in zip(missing, results):
            a, b = pairs[idx]
            legs[idx] = Leg(
                id=new_id(), sequence=0, from_lat=a[0], from_lon=a[1], to_lat=b[0], to_lon=b[1],
                mode=mode, provider=r.provider, status=r.status, distance_meters=r.distance_meters,
                duration_seconds=r.duration_seconds, geometry=r.geometry, fetched_at=r.fetched_at,
                error_code=r.error_code,
            )
    for i, leg in enumerate(legs, start=1):
        leg.sequence = i
    day.legs = legs  # type: ignore[assignment]
    return len(missing)


def schedule_day(day: Day, daily_start: str) -> None:
    """Derive arrival/departure times from routed durations + visit minutes.

    A leg without a routed duration adds no time (never fabricated); the
    validator flags the day's times as approximate in that case.
    """
    t = float(parse_hhmm(daily_start))
    for i, stop in enumerate(day.stops):
        leg = day.legs[i] if i < len(day.legs) else None
        if leg and leg.duration_seconds is not None:
            t += leg.duration_seconds / 60
        t = math.ceil(t / 5) * 5  # arrive on a 5-minute mark
        if stop.earliest_start:  # e.g. dinner not before 19:00 — free time until then
            t = max(t, parse_hhmm(stop.earliest_start))
        stop.planned_arrival = fmt_hhmm(t)
        t += stop.visit_minutes
        stop.planned_departure = fmt_hhmm(t)


# ------------------------------------------------------------------ deterministic fallback

def fallback_plan(candidates: list[Poi], day_count: int, pace: Pace,
                  start: tuple[float, float], daily_end: str = "20:00") -> ItineraryPlan:
    """Plan without the model: best-scored candidates, clustered by direction, ordered by proximity."""
    _, _, target = PACE_STOPS[pace]
    must = [c for c in candidates if c.must_see]
    rest = [c for c in candidates if not c.must_see]
    picked: list[Poi] = []
    food_count = 0
    for c in must + rest:
        if len(picked) >= day_count * target:
            break
        if c.interest in ("food", "nightlife") and not c.must_see:
            if food_count >= 2 * day_count:
                continue
            food_count += 1
        picked.append(c)

    # Cluster by compass direction from the start so each day stays in one area.
    picked.sort(key=lambda c: bearing_deg(start[0], start[1], c.lat, c.lon))
    per_day = math.ceil(len(picked) / day_count) if picked else 0
    days = []
    for d in range(day_count):
        chunk = picked[d * per_day:(d + 1) * per_day]
        # Sights by proximity; the first meal goes mid-day (lunch), other meals and bars end the day.
        ordered = nearest_neighbour([c for c in chunk if c.interest not in ("food", "nightlife")], start)
        meals = [c for c in chunk if c.interest == "food"]
        evening = meals[1:] + [c for c in chunk if c.interest == "nightlife"]
        if meals:
            ordered.insert(max(1, len(ordered) // 2) if ordered else 0, meals[0])
        ordered += evening
        late_day = parse_hhmm(daily_end) >= 20 * 60

        def not_before(c: Poi) -> Optional[str]:
            if meals and c is meals[0]:
                return "12:00"  # lunch
            if late_day and evening and c is evening[0]:
                return "18:30"  # dinner / evening out
            return None

        days.append(AgentDay(
            day_number=d + 1,
            stops=[AgentStop(candidate_id=c.id, visit_minutes=default_visit_minutes(c.category),
                             reason="Well-documented place near your other stops", earliest_start=not_before(c))
                   for c in ordered],
            summary="Nearby highlights grouped to keep travel short.",
        ))
    return ItineraryPlan(
        days=days,
        summary="Here's a plan built from well-documented places close to each other.",
        assumptions=["Opening hours were not verified."],
    )
