import sqlite3
from contextlib import closing

from backend.domain.models import (
    ChatMessage, Day, Location, OriginMode, Pace, Trip, TripConstraints, TripStatus, TransportMode, utc_now,
)
from backend.services import scheduling as sch
from backend.tests.conftest import POIS, FakeRouter


def sample_trip() -> Trip:
    origin = Location(label="Hotel", lat=38.716, lon=-9.141)
    day = Day(id="day1", day_number=1, trip_date="2026-10-01",
              stops=[sch.poi_to_stop(p, 45) for p in POIS[:3]])
    sch.renumber(day.stops)
    sch.route_day(day, origin, TransportMode.walking, True, FakeRouter())
    sch.schedule_day(day, "10:00")
    now = utc_now()
    return Trip(id="t1", title="Test", status=TripStatus.generated, destination=origin, origin=origin,
                origin_mode=OriginMode.search, start_date="2026-10-01", end_date="2026-10-01",
                daily_start_time="10:00", daily_end_time="20:00", transport_mode=TransportMode.walking,
                pace=Pace.moderate, interests=["culture"], constraints=TripConstraints(),
                days=[day], candidates=POIS[:5], chat=[ChatMessage(role="assistant", content="hi")],
                created_at=now, updated_at=now)


def test_round_trip(repo):
    trip = sample_trip()
    repo.save_trip(trip)
    loaded = repo.get_trip("t1")
    assert loaded.model_dump() == trip.model_dump()


def test_save_replaces_days_and_cascade_delete(repo):
    trip = sample_trip()
    repo.save_trip(trip)
    trip.days[0].stops = trip.days[0].stops[:1]
    trip.status = TripStatus.saved
    repo.save_trip(trip)
    assert len(repo.get_trip("t1").days[0].stops) == 1
    assert [t.id for t in repo.list_trips()] == ["t1"]

    assert repo.delete_trip("t1")
    with closing(sqlite3.connect(repo.path)) as conn:
        for table in ("itinerary_days", "stops", "route_legs"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert repo.get_trip("t1") is None
