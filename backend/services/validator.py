"""Deterministic, backend-enforced plan validation. The model is never trusted to self-validate."""

from difflib import SequenceMatcher

from backend.domain.geo import haversine_m
from backend.domain.models import (
    PACE_STOPS, Day, Issue, LegStatus, Pace, Poi, Severity, TransportMode, parse_hhmm,
)

LONG_LEG_SECONDS = {TransportMode.walking: 40 * 60, TransportMode.driving: 60 * 60}
NEAR_DUPLICATE_M = 150


def _similar(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.85


def day_end_minutes(day: Day, return_to_start: bool) -> float | None:
    """Minutes after midnight when the day ends (after the return leg, if any)."""
    if not day.stops or not day.stops[-1].planned_departure:
        return None
    end = parse_hhmm(day.stops[-1].planned_departure)
    # planned times wrap at midnight; unwrap relative to the first arrival
    first = parse_hhmm(day.stops[0].planned_arrival or day.stops[0].planned_departure)
    if end < first:
        end += 24 * 60
    if return_to_start and len(day.legs) > len(day.stops):
        back = day.legs[len(day.stops)]
        if back.duration_seconds is not None:
            end += back.duration_seconds / 60
    return end


def validate_day(day: Day, *, pace: Pace, mode: TransportMode, daily_start: str, daily_end: str,
                 return_to_start: bool) -> None:
    """Populate day.issues and each stop.issues in place."""
    day_issues: list[Issue] = []
    for s in day.stops:
        s.issues = []

    # Stop count vs pace
    lo, hi, _ = PACE_STOPS[pace]
    n = len(day.stops)
    if n == 0:
        day_issues.append(Issue(code="empty_day", severity=Severity.warning,
                                message="No stops planned for this day."))
    elif n < lo or n > hi:
        day_issues.append(Issue(
            code="pace_stop_count", severity=Severity.warning,
            message=f"{n} stop{'s' if n != 1 else ''} planned; a {pace.value} pace usually has {lo}–{hi}."))

    # Daily window overflow
    end = day_end_minutes(day, return_to_start)
    window_end = parse_hhmm(daily_end)
    if end is not None and end > window_end:
        over = int(round(end - window_end))
        day_issues.append(Issue(
            code="day_overflow", severity=Severity.error,
            message=f"This day runs about {over} min past your {daily_end} end time. "
                    "Remove a stop, shorten a visit, or extend the day."))

    # Legs: missing routes and long legs
    unknown_after = None
    for leg in day.legs:
        target = day.stops[leg.sequence - 1].name if leg.sequence <= n else "your start point"
        if leg.status != LegStatus.routed:
            if unknown_after is None:
                unknown_after = leg.sequence
            day_issues.append(Issue(
                code="route_unavailable", severity=Severity.warning,
                message=f"No route found to {target}; shown as a straight line and travel time is unknown.",
                stop_id=day.stops[leg.sequence - 1].id if leg.sequence <= n else None))
        elif leg.duration_seconds and leg.duration_seconds > LONG_LEG_SECONDS[mode]:
            mins = int(round(leg.duration_seconds / 60))
            day_issues.append(Issue(
                code="long_leg", severity=Severity.warning,
                message=f"Long {'walk' if mode == TransportMode.walking else 'drive'} to {target} (~{mins} min).",
                stop_id=day.stops[leg.sequence - 1].id if leg.sequence <= n else None))
    if unknown_after is not None:
        day_issues.append(Issue(
            code="times_approximate", severity=Severity.info,
            message="Times after a leg without a route don't include that travel time, so they may run later."))

    # Duplicates and near-duplicates
    seen: dict[str, str] = {}
    for i, s in enumerate(day.stops):
        if s.candidate_id in seen:
            s.issues.append(Issue(code="duplicate_stop", severity=Severity.error,
                                  message="This place appears more than once today.", stop_id=s.id))
        seen[s.candidate_id] = s.id
        for other in day.stops[:i]:
            if other.candidate_id != s.candidate_id and _similar(s.name, other.name) and \
                    haversine_m(s.lat, s.lon, other.lat, other.lon) < NEAR_DUPLICATE_M:
                s.issues.append(Issue(code="near_duplicate", severity=Severity.warning,
                                      message=f"Looks similar to “{other.name}” nearby — possibly the same place.",
                                      stop_id=s.id))

    # Opening hours are never verified
    for s in day.stops:
        hours = s.tags.get("opening_hours")
        if hours:
            s.issues.append(Issue(code="hours_unverified", severity=Severity.info,
                                  message=f"Listed hours (OSM, unverified): {hours}", stop_id=s.id))
        else:
            s.issues.append(Issue(code="hours_unknown", severity=Severity.info,
                                  message="Opening hours unknown — check before you go.", stop_id=s.id))

    # Visit starts before the daily window (should not happen, defensive)
    start = parse_hhmm(daily_start)
    if day.stops and day.stops[0].planned_arrival and parse_hhmm(day.stops[0].planned_arrival) < start:
        day_issues.append(Issue(code="starts_early", severity=Severity.warning,
                                message="The first stop starts before your daily start time."))

    day.issues = day_issues


def trip_level_issues(days: list[Day], must_see_pois: list[Poi], must_see_missing: list[str]) -> list[str]:
    """Human-readable trip-wide warnings (not stored per day)."""
    warnings = []
    planned = {s.candidate_id for d in days for s in d.stops}
    for poi in must_see_pois:
        if poi.id not in planned:
            warnings.append(f"Must-see “{poi.name}” was found but isn't in the plan.")
    for name in must_see_missing:
        warnings.append(f"Couldn't find “{name}” near the destination in OpenStreetMap.")
    counts: dict[str, int] = {}
    for d in days:
        for s in d.stops:
            counts[s.candidate_id] = counts.get(s.candidate_id, 0) + 1
    if any(c > 1 for c in counts.values()):
        warnings.append("Some places appear on more than one day.")
    return warnings


def has_blocking_errors(days: list[Day]) -> list[str]:
    """Violations worth one repair round-trip to the agent."""
    out = []
    for d in days:
        for i in d.issues:
            if i.code in ("day_overflow", "pace_stop_count", "empty_day"):
                out.append(f"Day {d.day_number}: {i.message}")
    return out
