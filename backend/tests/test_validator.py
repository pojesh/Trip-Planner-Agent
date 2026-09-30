from backend.domain.models import Day, Leg, LegStatus, Location, Pace, TransportMode
from backend.services import scheduling as sch
from backend.services.validator import validate_day
from backend.tests.conftest import FakeRouter, make_poi

ORIGIN = Location(label="Hotel", lat=38.716, lon=-9.141)


def build_day(pois, minutes=60, router=None, back=True):
    day = Day(id="d1", day_number=1, trip_date="2026-10-01",
              stops=[sch.poi_to_stop(p, minutes) for p in pois])
    sch.renumber(day.stops)
    sch.route_day(day, ORIGIN, TransportMode.walking, back, router or FakeRouter())
    sch.schedule_day(day, "10:00")
    return day


def validate(day, pace=Pace.moderate, end="20:00"):
    validate_day(day, pace=pace, mode=TransportMode.walking, daily_start="10:00", daily_end=end,
                 return_to_start=True)
    return {i.code for i in day.issues}


def test_feasible_day_has_no_errors():
    pois = [make_poi(f"node/{i}", 38.717 + i * 0.001, -9.141) for i in range(4)]
    codes = validate(build_day(pois))
    assert "day_overflow" not in codes and "pace_stop_count" not in codes


def test_overflow_is_flagged():
    pois = [make_poi(f"node/{i}", 38.717 + i * 0.001, -9.141) for i in range(5)]
    assert "day_overflow" in validate(build_day(pois, minutes=150))


def test_pace_bounds():
    pois = [make_poi("node/1", 38.717, -9.141)]
    assert "pace_stop_count" in validate(build_day(pois), pace=Pace.moderate)


def test_long_walk_flagged():
    pois = [make_poi("node/1", 38.80, -9.141), make_poi("node/2", 38.801, -9.141), make_poi("node/3", 38.802, -9.141)]
    assert "long_leg" in validate(build_day(pois))


def test_fallback_leg_warns_and_adds_no_time():
    pois = [make_poi("node/1", 38.72, -9.141), make_poi("node/2", 38.721, -9.141), make_poi("node/3", 38.722, -9.141)]
    day = build_day(pois, router=FakeRouter(fail=True))
    assert all(l.status == LegStatus.fallback and l.duration_seconds is None for l in day.legs)
    assert day.stops[0].planned_arrival == "10:00"  # no fabricated travel time
    codes = validate(day)
    assert {"route_unavailable", "times_approximate"} <= codes


def test_duplicates_and_hours():
    p = make_poi("node/1", 38.717, -9.141, hours="Tu-Su 10:00-18:00")
    day = build_day([p, p, make_poi("node/2", 38.718, -9.141)])
    validate(day)
    stop_codes = [{i.code for i in s.issues} for s in day.stops]
    assert "duplicate_stop" in stop_codes[1]
    assert "hours_unverified" in stop_codes[0]
    assert "hours_unknown" in stop_codes[2]


def test_edit_reuses_unchanged_legs():
    router = FakeRouter()
    pois = [make_poi(f"node/{i}", 38.717 + i * 0.001, -9.141) for i in range(4)]
    day = build_day(pois, router=router)
    assert router.legs_routed == 5  # 4 stops + return
    router.legs_routed = 0
    day.stops.pop(3)  # remove the last stop
    sch.renumber(day.stops)
    rerouted = sch.route_day(day, ORIGIN, TransportMode.walking, True, router)
    assert rerouted == 1 and router.legs_routed == 1  # only the new "back to start" leg
    assert len(day.legs) == 4


def test_earliest_start_waits_for_dinner():
    pois = [make_poi("node/1", 38.717, -9.141), make_poi("node/2", 38.718, -9.141, interest="food")]
    day = Day(id="d1", day_number=1, trip_date="2026-10-01", stops=[
        sch.poi_to_stop(pois[0], 60), sch.poi_to_stop(pois[1], 75, earliest_start="19:00")])
    sch.renumber(day.stops)
    sch.route_day(day, ORIGIN, TransportMode.walking, True, FakeRouter())
    sch.schedule_day(day, "10:00")
    assert day.stops[0].planned_arrival == "10:05"
    assert (day.stops[1].planned_arrival, day.stops[1].planned_departure) == ("19:00", "20:15")
    assert "day_overflow" in validate(day)  # 20:15 + walk back > 20:00


def test_leg_sequence_and_legless_day():
    day = build_day([])
    assert day.legs == [] and "empty_day" in validate(day)
