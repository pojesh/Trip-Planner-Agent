"""Small geographic helpers (no external dependencies)."""

import math

EARTH_RADIUS_M = 6_371_000


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing from point 1 to point 2, 0..360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def valid_coord(lat, lon) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if math.isnan(lat) or math.isnan(lon):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def path_length_m(points: list[tuple[float, float]]) -> float:
    return sum(haversine_m(*points[i], *points[i + 1]) for i in range(len(points) - 1))


def bbox_around(lat: float, lon: float, radius_m: float) -> list[float]:
    """[south, north, west, east] box enclosing a circle."""
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * max(math.cos(math.radians(lat)), 0.01))
    return [lat - dlat, lat + dlat, lon - dlon, lon + dlon]


MIN_RADIUS_M = 5_000
MAX_RADIUS_M = 15_000
DAY_TRIP_RADIUS_M = 100_000  # how far away a named day-trip town / must-see may be


def radius_for_bbox(bbox: list[float], min_m: int = MIN_RADIUS_M, max_m: int = MAX_RADIUS_M) -> int:
    """Search radius from a Nominatim bbox [south, north, west, east]: 0.8 × half its diagonal."""
    if len(bbox) != 4:
        return 8_000
    south, north, west, east = bbox
    half_diag = haversine_m(south, west, north, east) / 2
    return int(max(min_m, min(max_m, half_diag * 0.8)))
