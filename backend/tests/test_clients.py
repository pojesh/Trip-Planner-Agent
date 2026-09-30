import httpx
import pytest

from backend.clients.nominatim import NominatimClient
from backend.clients.overpass import build_query, parse_elements
from backend.clients.routing import RoutingClient
from backend.domain.models import LegStatus, TransportMode
from backend.errors import AppError

# ------------------------------------------------------------------ Overpass

OVERPASS_FIXTURE = {"elements": [
    {"type": "node", "id": 1, "lat": 38.70, "lon": -9.14, "tags": {"name": "Museu A", "tourism": "museum", "wikidata": "Q1"}},
    {"type": "way", "id": 2, "center": {"lat": 38.71, "lon": -9.15}, "tags": {"name": "Castelo", "historic": "castle"}},
    {"type": "node", "id": 1, "lat": 38.70, "lon": -9.14, "tags": {"name": "Museu A", "tourism": "museum"}},  # dup
    {"type": "node", "id": 3, "lat": 38.70, "lon": -9.14, "tags": {"tourism": "museum"}},  # no name
    {"type": "way", "id": 4, "tags": {"name": "No centre", "tourism": "museum"}},  # no coords
    {"type": "node", "id": 5, "lat": 38.70, "lon": -9.14, "tags": {"name": "Private garden", "leisure": "garden", "access": "private"}},
    {"type": "node", "id": 7, "lat": 38.70, "lon": -9.14, "tags": {"name": "Tasca", "amenity": "restaurant"}},
]}


def test_overpass_parse_dedupe_and_classify():
    pois = parse_elements(OVERPASS_FIXTURE, 38.71, -9.14, 5000, ["culture", "history", "outdoors"])
    ids = [p.id for p in pois]
    assert ids.count("node/1") == 1
    assert "way/2" in ids and "node/3" not in ids and "way/4" not in ids and "node/5" not in ids
    assert "node/7" not in ids  # food not requested
    castle = next(p for p in pois if p.id == "way/2")
    assert castle.category == "Castle" and castle.lat == 38.71
    assert castle.source_url == "https://www.openstreetmap.org/way/2"
    assert pois[0].id == "node/1"  # wikidata-tagged museum ranks first


def test_overpass_query_is_bounded():
    q = build_query(38.7, -9.1, 5000, ["culture", "food"])
    assert q.count("around:5000,38.700000,-9.100000") == 3  # every clause (2 culture + 1 food) is bounded
    assert ".culture out center" in q and ".food out center" in q


# ------------------------------------------------------------------ OSRM

def _osrm_ok(request):
    step = lambda a, b: {"geometry": {"coordinates": [a, b]}}
    return httpx.Response(200, json={"code": "Ok", "routes": [{"legs": [
        {"distance": 800, "duration": 600, "steps": [step([-9.14, 38.71], [-9.13, 38.72])]},
        {"distance": 500, "duration": 400, "steps": [step([-9.13, 38.72], [-9.12, 38.73])]},
    ]}]})


def test_routing_success_uses_foot_profile():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return _osrm_ok(request)

    rc = RoutingClient("https://routing.openstreetmap.de", "test", 5, http=httpx.Client(transport=httpx.MockTransport(handler)))
    legs = rc.route([(38.71, -9.14), (38.72, -9.13), (38.73, -9.12)], TransportMode.walking)
    assert "/routed-foot/route/v1/foot/" in seen[0]
    assert [l.status for l in legs] == [LegStatus.routed, LegStatus.routed]
    assert legs[0].duration_seconds == 600 and legs[1].distance_meters == 500
    # cached: a second identical call makes no request
    rc.route([(38.71, -9.14), (38.72, -9.13), (38.73, -9.12)], TransportMode.walking)
    assert len(seen) == 1


def test_routing_no_route_is_fallback_without_duration():
    rc = RoutingClient("https://example.org", "test", 5, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": "NoRoute"}))))
    legs = rc.route([(38.71, -9.14), (38.72, -9.13)], TransportMode.driving)
    assert legs[0].status == LegStatus.fallback and legs[0].duration_seconds is None
    assert legs[0].error_code == "noroute" and legs[0].distance_meters > 0


def test_routing_timeout_is_fallback():
    def boom(request):
        raise httpx.ConnectTimeout("slow")
    rc = RoutingClient("https://example.org", "test", 5, http=httpx.Client(transport=httpx.MockTransport(boom)))
    legs = rc.route([(38.71, -9.14), (38.72, -9.13)], TransportMode.walking)
    assert legs[0].status == LegStatus.fallback and legs[0].error_code == "provider_timeout"


# ------------------------------------------------------------------ Nominatim

NOMINATIM_FIXTURE = [{"lat": "38.7077", "lon": "-9.1365", "display_name": "Lisbon, Portugal", "name": "Lisbon",
                      "type": "city", "category": "boundary", "osm_type": "relation", "osm_id": 5400890,
                      "boundingbox": ["38.69", "38.80", "-9.23", "-9.09"], "address": {"country": "Portugal"}},
                     {"lat": "bad", "lon": "x", "display_name": "broken"}]


def test_nominatim_maps_and_caches():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["format"] == "jsonv2"
        return httpx.Response(200, json=NOMINATIM_FIXTURE)

    nc = NominatimClient("https://nominatim.example", "test", 5, http=httpx.Client(transport=httpx.MockTransport(handler)))
    res = nc.search_place("  Lisbon ")
    assert len(res) == 1 and res[0].name == "Lisbon" and res[0].country == "Portugal"
    assert res[0].boundingbox == [38.69, 38.80, -9.23, -9.09]
    nc.search_place("lisbon")
    assert len(calls) == 1


def test_nominatim_searches_nearby_first_then_worldwide():
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        bounded = request.url.params.get("bounded") == "1"
        return httpx.Response(200, json=[] if bounded else NOMINATIM_FIXTURE[:1])

    nc = NominatimClient("https://nominatim.example", "test", 5, http=httpx.Client(transport=httpx.MockTransport(handler)))
    bbox = [38.69, 38.80, -9.23, -9.09]
    assert len(nc.search_place("somewhere", near_bbox=bbox)) == 1
    assert calls[0]["bounded"] == "1" and "viewbox" not in calls[1]
    # must-see lookups never leave the destination area
    found, missing = nc.find_must_see(["Nowhere Tower"], bbox)
    assert found == [] and missing == ["Nowhere Tower"]


def test_nominatim_rate_limited():
    nc = NominatimClient("https://nominatim.example", "test", 5, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "1"}))))
    with pytest.raises(AppError) as e:
        nc.search_place("paris")
    assert e.value.code == "provider_busy" and e.value.retryable
