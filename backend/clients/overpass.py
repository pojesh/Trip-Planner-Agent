"""Overpass (OpenStreetMap) client: bounded discovery of candidate places.

Interest -> OSM tag mapping lives in CLAUSES so it is easy to audit. One union
query covers all requested interests (public Overpass allows ~2 concurrent
requests per IP), and elements are classified locally by their tags.
"""

from typing import Iterable, Optional

import httpx

from backend.clients.http import request_json
from backend.domain.geo import haversine_m, valid_coord
from backend.domain.models import Poi, utc_now

# interest -> [(tag key, allowed values, extra required tags)]
CLAUSES: dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]] = {
    "culture": [("tourism", ("museum", "gallery"), ()),
                ("amenity", ("theatre", "arts_centre"), ())],
    "history": [("historic", ("castle", "monument", "ruins", "archaeological_site",
                              "fort", "palace", "city_gate", "manor", "tower"), ()),
                ("historic", ("memorial",), ("wikidata",))],
    "landmarks": [("tourism", ("attraction", "viewpoint"), ())],
    "outdoors": [("leisure", ("park", "garden", "nature_reserve"), ()),
                 ("natural", ("beach",), ())],
    "food": [("amenity", ("restaurant", "cafe"), ())],
    "nightlife": [("amenity", ("bar", "pub", "biergarten"), ())],
    "shopping": [("amenity", ("marketplace",), ()),
                 ("shop", ("mall", "department_store"), ())],
    "religious": [("amenity", ("place_of_worship",), ("wikidata",))],
}

PER_INTEREST_RAW = 100   # elements requested from Overpass per interest
PER_INTEREST_CAP = 20    # candidates kept per interest
TOTAL_CAP = 80           # candidates kept overall


class OverpassClient:
    def __init__(self, base_url: str, fallback_url: str, user_agent: str, timeout_s: float,
                 http: Optional[httpx.Client] = None):
        self.urls = [base_url, fallback_url]
        self.timeout_s = timeout_s
        self.http = http or httpx.Client(timeout=timeout_s + 5, headers={"User-Agent": user_agent})
        self._cache: dict[tuple, list[Poi]] = {}

    def find_pois(self, center_lat: float, center_lon: float, radius_m: int,
                  interests: Iterable[str]) -> list[Poi]:
        interests = [i for i in dict.fromkeys(interests) if i in CLAUSES]
        if not interests:
            return []
        key = (round(center_lat, 4), round(center_lon, 4), radius_m, tuple(interests))
        if key in self._cache:
            return self._cache[key]

        query = build_query(center_lat, center_lon, radius_m, interests, int(self.timeout_s))
        data = request_json(self.http, "POST", self.urls, provider="Overpass", data={"data": query})
        pois = parse_elements(data, center_lat, center_lon, radius_m, interests)
        self._cache[key] = pois
        return pois


def build_query(lat: float, lon: float, radius_m: int, interests: list[str], timeout_s: int = 25) -> str:
    around = f"around:{int(radius_m)},{lat:.6f},{lon:.6f}"
    parts = [f"[out:json][timeout:{max(5, timeout_s)}];"]
    for interest in interests:
        body = "".join(
            f'nwr["{key}"~"^({"|".join(values)})$"]["name"]{"".join(f"""["{e}"]""" for e in extra)}({around});'
            for key, values, extra in CLAUSES[interest])
        parts.append(f"({body})->.{interest};.{interest} out center {PER_INTEREST_RAW};")
    return "\n".join(parts)


def classify(tags: dict, interests: list[str]) -> Optional[tuple[str, str]]:
    """Return (interest, category label) for the first matching requested interest."""
    for interest in interests:
        for key, values, extra in CLAUSES[interest]:
            if tags.get(key) in values and all(e in tags for e in extra):
                return interest, tags[key].replace("_", " ").capitalize()
    return None


def _address(tags: dict) -> Optional[str]:
    street = tags.get("addr:street")
    if not street:
        return None
    line = f"{street} {tags.get('addr:housenumber', '')}".strip()
    city = tags.get("addr:city")
    return f"{line}, {city}" if city else line


def _score(tags: dict, dist_m: float, radius_m: int) -> float:
    """Notability first (well-documented places are usually worth visiting), then closeness."""
    s = 0.0
    s += 3 if "wikidata" in tags else 0
    s += 2 if "wikipedia" in tags else 0
    s += 1 if ("website" in tags or "contact:website" in tags) else 0
    s += 0.5 if "opening_hours" in tags else 0
    s += 1 if "heritage" in tags else 0
    s += 0.5 if "tourism" in tags else 0
    return s - 1.5 * dist_m / max(radius_m, 1)


def parse_elements(data, center_lat: float, center_lon: float, radius_m: int,
                   interests: list[str]) -> list[Poi]:
    elements = data.get("elements", []) if isinstance(data, dict) else []
    fetched_at = utc_now()
    seen: set[str] = set()
    by_interest: dict[str, list[Poi]] = {i: [] for i in interests}

    for el in elements:
        if not isinstance(el, dict):
            continue
        tags = el.get("tags") or {}
        name = (tags.get("name:en") or tags.get("name") or "").strip()  # English name when mapped
        osm_type, osm_id = el.get("type"), el.get("id")
        if not name or osm_type not in ("node", "way", "relation") or osm_id is None:
            continue
        if tags.get("access") in ("private", "no"):
            continue
        if osm_type == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            center = el.get("center") or {}
            lat, lon = center.get("lat"), center.get("lon")
        pid = f"{osm_type}/{osm_id}"
        cls = classify(tags, interests)
        if not valid_coord(lat, lon) or pid in seen or cls is None:
            continue
        seen.add(pid)
        dist = haversine_m(center_lat, center_lon, float(lat), float(lon))
        interest, category = cls
        by_interest[interest].append(Poi(
            id=pid, osm_type=osm_type, osm_id=str(osm_id), name=name,
            lat=float(lat), lon=float(lon), category=category, interest=interest,
            address=_address(tags),
            opening_hours=tags.get("opening_hours"),
            website=tags.get("website") or tags.get("contact:website"),
            wikidata=tags.get("wikidata"), wikipedia=tags.get("wikipedia"),
            tags={k: str(v) for k, v in tags.items() if not k.startswith(("source", "note", "fixme"))},
            source_url=f"https://www.openstreetmap.org/{pid}",
            fetched_at=fetched_at,
            score=round(_score(tags, dist, radius_m), 3),
        ))

    # Keep the best per interest, then round-robin for diversity up to the total cap.
    ranked = {i: sorted(lst, key=lambda p: -p.score)[:PER_INTEREST_CAP] for i, lst in by_interest.items()}
    result: list[Poi] = []
    while len(result) < TOTAL_CAP and any(ranked.values()):
        for interest in interests:
            if ranked[interest] and len(result) < TOTAL_CAP:
                result.append(ranked[interest].pop(0))
    return result
