"""Nominatim (OpenStreetMap) geocoding — called only on explicit user submit."""

import threading
import time
from typing import Optional

import httpx

from backend.clients.http import request_json
from backend.domain.geo import valid_coord
from backend.domain.models import PlaceResult, Poi, utc_now

MIN_INTERVAL_S = 1.0  # Nominatim usage policy: max 1 request/second


class NominatimClient:
    def __init__(self, base_url: str, user_agent: str, timeout_s: float,
                 http: Optional[httpx.Client] = None):
        self.base_url = base_url
        self.http = http or httpx.Client(timeout=timeout_s, headers={"User-Agent": user_agent})
        self._cache: dict[tuple, list[PlaceResult]] = {}
        self._lock = threading.Lock()
        self._last_call = 0.0

    def search_place(self, query: str, limit: int = 5, near_bbox: Optional[list[float]] = None,
                     only_nearby: bool = False) -> list[PlaceResult]:
        """Search by name. With `near_bbox`, look inside that area first; unless
        `only_nearby`, fall back to a worldwide search when nothing is found there."""
        if near_bbox and len(near_bbox) == 4:
            results = self._search(query, limit, near_bbox)
            if results or only_nearby:
                return results
        return self._search(query, limit, None)

    def find_must_see(self, names: list[str], bbox: list[float]) -> tuple[list[Poi], list[str]]:
        """Look up named must-see places inside the destination area. Returns (found, missing)."""
        found, missing = [], []
        for name in names[:5]:
            hits = self.search_place(name, limit=1, near_bbox=bbox, only_nearby=True)
            if hits:
                found.append(place_to_poi(hits[0]))
            else:
                missing.append(name)
        return found, missing

    def _search(self, query: str, limit: int, bbox: Optional[list[float]]) -> list[PlaceResult]:
        norm = " ".join(query.lower().split())
        limit = max(1, min(limit, 5))
        key = (norm, limit, tuple(round(x, 3) for x in bbox) if bbox else None)
        if key in self._cache:
            return self._cache[key]

        params = {"q": norm, "format": "jsonv2", "addressdetails": 1, "extratags": 1, "limit": limit,
                  "accept-language": "en"}  # English names where OSM has them
        if bbox:
            south, north, west, east = bbox
            params["viewbox"] = f"{west},{north},{east},{south}"  # left,top,right,bottom
            params["bounded"] = 1

        with self._lock:  # serialise + rate limit
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                data = request_json(self.http, "GET", [f"{self.base_url}/search"],
                                    provider="Nominatim", params=params)
            finally:
                self._last_call = time.monotonic()

        results = parse_results(data)
        self._cache[key] = results
        return results


def parse_results(data) -> list[PlaceResult]:
    if not isinstance(data, list):
        return []
    out: list[PlaceResult] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        lat, lon = item.get("lat"), item.get("lon")
        if not valid_coord(lat, lon):
            continue
        address = item.get("address") or {}
        label = item.get("display_name") or item.get("name") or ""
        name = item.get("name") or label.split(",")[0]
        try:
            bbox = [float(x) for x in item.get("boundingbox") or []]
        except (TypeError, ValueError):
            bbox = []
        out.append(PlaceResult(
            label=label,
            name=name,
            lat=float(lat),
            lon=float(lon),
            type=str(item.get("addresstype") or item.get("type") or ""),  # "city"/"state" beats "administrative"
            place_class=str(item.get("category") or item.get("class") or ""),
            osm_type=str(item.get("osm_type") or ""),
            osm_id=str(item.get("osm_id") or ""),
            boundingbox=bbox if len(bbox) == 4 else [],
            country=str(address.get("country") or ""),
            tags={str(k): str(v) for k, v in (item.get("extratags") or {}).items()},
        ))
    return out


def place_to_poi(place: PlaceResult) -> Poi:
    """Turn a Nominatim hit (e.g. a must-see place) into a sourced candidate."""
    pid = f"{place.osm_type}/{place.osm_id}"
    tags = {"name": place.name, **place.tags}
    return Poi(
        id=pid, osm_type=place.osm_type, osm_id=place.osm_id, name=place.name,
        lat=place.lat, lon=place.lon, category=(place.type or "place").replace("_", " ").capitalize(),
        interest="must_see", opening_hours=tags.get("opening_hours"), website=tags.get("website"),
        wikidata=tags.get("wikidata"), wikipedia=tags.get("wikipedia"), tags=tags,
        source_url=f"https://www.openstreetmap.org/{pid}", fetched_at=utc_now(), must_see=True, score=10,
    )
