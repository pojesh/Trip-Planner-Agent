"""OSRM routing adapter.

Default host is the FOSSGIS server (routing.openstreetmap.de), which exposes
real foot and car profiles under /routed-foot and /routed-car. Any other
OSRM-compatible host is used as /route/v1/{foot|driving}/...

Never fabricates travel times: a failed leg becomes 'fallback' with a
straight-line geometry and NO duration.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from backend.clients.http import request_json
from backend.domain.geo import haversine_m, valid_coord
from backend.domain.models import LegStatus, TransportMode, utc_now
from backend.errors import AppError

log = logging.getLogger(__name__)

Point = tuple[float, float]  # (lat, lon)


@dataclass
class RouteResult:
    status: LegStatus
    distance_meters: Optional[float] = None
    duration_seconds: Optional[float] = None
    geometry: Optional[list[list[float]]] = None  # [[lon, lat], ...]
    provider: Optional[str] = None
    fetched_at: Optional[str] = None
    error_code: Optional[str] = None


def fallback_leg(a: Point, b: Point, error_code: str) -> RouteResult:
    if not (valid_coord(*a) and valid_coord(*b)):
        return RouteResult(status=LegStatus.unavailable, error_code="invalid_coordinates")
    return RouteResult(
        status=LegStatus.fallback,
        distance_meters=round(haversine_m(*a, *b), 1),  # straight-line, labelled as such in the UI
        duration_seconds=None,
        geometry=[[a[1], a[0]], [b[1], b[0]]],
        error_code=error_code,
    )


class RoutingClient:
    PROVIDER = "OSRM"

    def __init__(self, base_url: str, user_agent: str, timeout_s: float,
                 http: Optional[httpx.Client] = None):
        self.base_url = base_url
        self.http = http or httpx.Client(timeout=timeout_s, headers={"User-Agent": user_agent})
        self._cache: dict[tuple, RouteResult] = {}

    def _url(self, mode: TransportMode, coords: str) -> str:
        if "routing.openstreetmap.de" in self.base_url:
            prefix, profile = ("routed-foot", "foot") if mode == TransportMode.walking else ("routed-car", "driving")
            return f"{self.base_url}/{prefix}/route/v1/{profile}/{coords}"
        profile = "foot" if mode == TransportMode.walking else "driving"
        return f"{self.base_url}/route/v1/{profile}/{coords}"

    @staticmethod
    def _key(a: Point, b: Point, mode: TransportMode) -> tuple:
        return (round(a[0], 5), round(a[1], 5), round(b[0], 5), round(b[1], 5), mode.value)

    def route(self, points: list[Point], mode: TransportMode, use_cache: bool = True) -> list[RouteResult]:
        """Route consecutive points; returns one RouteResult per leg (len(points) - 1)."""
        if len(points) < 2:
            return []
        pairs = list(zip(points[:-1], points[1:]))
        if use_cache and all(self._key(a, b, mode) in self._cache for a, b in pairs):
            return [self._cache[self._key(a, b, mode)] for a, b in pairs]
        if not all(valid_coord(*p) for p in points):
            return [fallback_leg(a, b, "invalid_coordinates") for a, b in pairs]

        coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
        params = {"overview": "false", "steps": "true", "geometries": "geojson"}
        try:
            data = request_json(self.http, "GET", [self._url(mode, coords)],
                                provider=self.PROVIDER, params=params)
            results = self._parse(data, pairs)
        except AppError as e:
            log.warning("routing failed code=%s", e.code)
            results = [fallback_leg(a, b, e.code) for a, b in pairs]

        for (a, b), r in zip(pairs, results):
            if r.status == LegStatus.routed:
                self._cache[self._key(a, b, mode)] = r
        return results

    def _parse(self, data, pairs: list[tuple[Point, Point]]) -> list[RouteResult]:
        if not isinstance(data, dict) or data.get("code") != "Ok" or not data.get("routes"):
            code = data.get("code", "bad_response") if isinstance(data, dict) else "bad_response"
            return [fallback_leg(a, b, str(code).lower()) for a, b in pairs]
        legs = data["routes"][0].get("legs") or []
        if len(legs) != len(pairs):
            return [fallback_leg(a, b, "leg_mismatch") for a, b in pairs]
        fetched = utc_now()
        out = []
        for (a, b), leg in zip(pairs, legs):
            coords: list[list[float]] = []
            for step in leg.get("steps") or []:
                for c in (step.get("geometry") or {}).get("coordinates") or []:
                    if not coords or coords[-1] != c:
                        coords.append(c)
            if len(coords) < 2:
                coords = [[a[1], a[0]], [b[1], b[0]]]
            try:
                distance, duration = float(leg["distance"]), float(leg["duration"])
            except (KeyError, TypeError, ValueError):
                out.append(fallback_leg(a, b, "bad_response"))
                continue
            out.append(RouteResult(
                status=LegStatus.routed, distance_meters=round(distance, 1),
                duration_seconds=round(duration, 1), geometry=coords,
                provider=self.PROVIDER, fetched_at=fetched,
            ))
        return out
