"""Shared fakes: no test touches the network or the real database."""

import pytest

from backend.clients.routing import RouteResult
from backend.domain.geo import haversine_m
from backend.domain.models import LegStatus, PlaceResult, Poi, utc_now
from backend.repositories.sqlite_repo import SqliteRepo


def make_poi(pid: str, lat: float, lon: float, interest: str = "culture", category: str = "Museum",
             must_see: bool = False, hours: str | None = None) -> Poi:
    osm_type, osm_id = pid.split("/")
    tags = {"name": f"Place {osm_id}"}
    if hours:
        tags["opening_hours"] = hours
    return Poi(id=pid, osm_type=osm_type, osm_id=osm_id, name=f"Place {osm_id}", lat=lat, lon=lon,
               category=category, interest=interest, opening_hours=hours, tags=tags,
               source_url=f"https://www.openstreetmap.org/{pid}", fetched_at=utc_now(), must_see=must_see)


# A small grid of places around Lisbon's centre.
POIS = [make_poi(f"node/{i}", 38.71 + (i % 4) * 0.004, -9.14 + (i // 4) * 0.004,
                 interest="food" if i % 5 == 0 else "culture",
                 category="Restaurant" if i % 5 == 0 else "Museum")
        for i in range(1, 17)]

DEST = PlaceResult(label="Lisbon, Portugal", name="Lisbon", lat=38.7223, lon=-9.1393, type="city",
                   place_class="boundary", osm_type="relation", osm_id="5400890",
                   boundingbox=[38.69, 38.80, -9.23, -9.09], country="Portugal")


class FakeNominatim:
    def search_place(self, query, limit=5, near_bbox=None, only_nearby=False):
        return [DEST]

    def find_must_see(self, names, bbox):
        found = [make_poi("way/900", 38.6916, -9.2160, interest="must_see", category="Tower", must_see=True)]
        return (found, names[1:]) if names else ([], [])


class FakeOverpass:
    def __init__(self, pois=None):
        self.pois = POIS if pois is None else pois
        self.calls = 0

    def find_pois(self, lat, lon, radius_m, interests):
        self.calls += 1
        return [p for p in self.pois if p.interest in interests]


class FakeRouter:
    """Walking at ~1.25 m/s along straight lines; records how many legs were routed."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.legs_routed = 0

    def route(self, points, mode, use_cache=True):
        out = []
        for a, b in zip(points[:-1], points[1:]):
            self.legs_routed += 1
            if self.fail:
                out.append(RouteResult(status=LegStatus.fallback, distance_meters=haversine_m(*a, *b),
                                       geometry=[[a[1], a[0]], [b[1], b[0]]], error_code="provider_timeout"))
            else:
                d = haversine_m(*a, *b)
                out.append(RouteResult(status=LegStatus.routed, distance_meters=d, duration_seconds=d / 1.25,
                                       geometry=[[a[1], a[0]], [b[1], b[0]]], provider="OSRM",
                                       fetched_at=utc_now()))
        return out


@pytest.fixture
def repo(tmp_path):
    r = SqliteRepo(tmp_path / "test.db")
    r.init()
    return r


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import backend.clients.http as http
    import backend.clients.nominatim as nom
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(nom.time, "sleep", lambda s: None)


def trip_request_json(**overrides) -> dict:
    body = {
        "destination": DEST.model_dump(),
        "origin": {"label": "Hotel Avenida", "lat": 38.716, "lon": -9.141},
        "origin_mode": "search",
        "start_date": "2026-10-01", "end_date": "2026-10-02",
        "daily_start_time": "10:00", "daily_end_time": "20:00",
        "interests": ["culture", "food"], "pace": "moderate", "transport_mode": "walking",
    }
    body.update(overrides)
    return body
