"""SQLite persistence for trips, days, stops and route legs.

A trip is always written as a whole (trip row + its days/stops/legs replaced)
inside one transaction, so a failed write never leaves a half-saved plan.
"""

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from backend.domain.models import (
    Booking, ChatMessage, Day, Issue, Leg, Location, Poi, Stop, Trip, TripConstraints, TripStatus, TripSummary,
)
from backend.errors import AppError

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','generated','saved','archived')),
  destination_label TEXT NOT NULL,
  destination_lat REAL NOT NULL,
  destination_lon REAL NOT NULL,
  origin_label TEXT NOT NULL,
  origin_lat REAL NOT NULL,
  origin_lon REAL NOT NULL,
  origin_mode TEXT NOT NULL CHECK(origin_mode IN ('search','pin')),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  daily_start_time TEXT NOT NULL,
  daily_end_time TEXT NOT NULL,
  transport_mode TEXT NOT NULL CHECK(transport_mode IN ('walking','driving')),
  pace TEXT NOT NULL CHECK(pace IN ('relaxed','moderate','packed')),
  interests_json TEXT NOT NULL,
  constraints_json TEXT NOT NULL,
  agent_summary TEXT,
  candidates_json TEXT NOT NULL DEFAULT '[]',
  chat_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS itinerary_days (
  id TEXT PRIMARY KEY,
  trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  day_number INTEGER NOT NULL,
  trip_date TEXT NOT NULL,
  start_location_label TEXT,
  summary TEXT,
  issues_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(trip_id, day_number)
);

CREATE TABLE IF NOT EXISTS stops (
  id TEXT PRIMARY KEY,
  itinerary_day_id TEXT NOT NULL REFERENCES itinerary_days(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL,
  name TEXT NOT NULL,
  category TEXT,
  osm_type TEXT,
  osm_id TEXT,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  address TEXT,
  tags_json TEXT NOT NULL DEFAULT '{}',
  planned_arrival TEXT,
  planned_departure TEXT,
  visit_minutes INTEGER,
  earliest_start TEXT,
  user_note TEXT,
  reason TEXT,
  source_url TEXT,
  source_fetched_at TEXT,
  validation_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(itinerary_day_id, sequence)
);

CREATE TABLE IF NOT EXISTS route_legs (
  id TEXT PRIMARY KEY,
  itinerary_day_id TEXT NOT NULL REFERENCES itinerary_days(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL,
  from_lat REAL NOT NULL, from_lon REAL NOT NULL,
  to_lat REAL NOT NULL, to_lon REAL NOT NULL,
  mode TEXT NOT NULL,
  provider TEXT,
  status TEXT NOT NULL CHECK(status IN ('routed','fallback','unavailable')),
  distance_meters REAL,
  duration_seconds REAL,
  geometry_json TEXT,
  fetched_at TEXT,
  error_code TEXT,
  UNIQUE(itinerary_day_id, sequence)
);

CREATE TABLE IF NOT EXISTS bookings (
  trip_id TEXT PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE,
  booking_ref TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('booked','cancelled')),
  trip_type TEXT NOT NULL CHECK(trip_type IN ('domestic','international')),
  fare_type TEXT NOT NULL CHECK(fare_type IN ('standard','flexi','promo')),
  policy_version TEXT NOT NULL,
  booked_at TEXT NOT NULL,
  cancelled_at TEXT,
  cancellation_ref TEXT UNIQUE,
  cancellation_reason TEXT,
  charge_percent INTEGER,
  refund_percent INTEGER,
  refund_status TEXT,
  refund_expected_by TEXT
);

-- A cancellation quote waiting for the user's confirmation.
CREATE TABLE IF NOT EXISTS pending_actions (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);

CREATE TABLE IF NOT EXISTS support_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  at TEXT NOT NULL
);

-- One row per support-assistant turn, shown on the Dev Board.
CREATE TABLE IF NOT EXISTS assistant_traces (
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  user_message TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  events_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_days_trip ON itinerary_days(trip_id);
CREATE INDEX IF NOT EXISTS idx_stops_day ON stops(itinerary_day_id, sequence);
CREATE INDEX IF NOT EXISTS idx_legs_day ON route_legs(itinerary_day_id, sequence);
"""


def _dump(model_list) -> str:
    return json.dumps([m.model_dump(mode="json") for m in model_list])


class SqliteRepo:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            # Tiny migration for databases created before `earliest_start` existed.
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(stops)")}
            if "earliest_start" not in columns:
                conn.execute("ALTER TABLE stops ADD COLUMN earliest_start TEXT")
                conn.commit()

    # ------------------------------------------------------------ writes

    def save_trip(self, trip: Trip) -> None:
        try:
            with closing(self._connect()) as conn, conn:  # `with conn` = one transaction
                self._write(conn, trip)
        except sqlite3.Error:
            raise AppError("storage_error", "Could not save the trip to local storage. "
                           "Your changes are NOT saved — please retry.", retryable=True, http_status=500)

    def _write(self, conn: sqlite3.Connection, t: Trip) -> None:
        conn.execute("DELETE FROM itinerary_days WHERE trip_id = ?", (t.id,))  # cascades
        conn.execute(
            """INSERT INTO trips (id, title, status, destination_label, destination_lat, destination_lon,
                 origin_label, origin_lat, origin_lon, origin_mode, start_date, end_date,
                 daily_start_time, daily_end_time, transport_mode, pace, interests_json,
                 constraints_json, agent_summary, candidates_json, chat_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, status=excluded.status,
                 destination_label=excluded.destination_label, destination_lat=excluded.destination_lat,
                 destination_lon=excluded.destination_lon, origin_label=excluded.origin_label,
                 origin_lat=excluded.origin_lat, origin_lon=excluded.origin_lon,
                 origin_mode=excluded.origin_mode, start_date=excluded.start_date,
                 end_date=excluded.end_date, daily_start_time=excluded.daily_start_time,
                 daily_end_time=excluded.daily_end_time, transport_mode=excluded.transport_mode,
                 pace=excluded.pace, interests_json=excluded.interests_json,
                 constraints_json=excluded.constraints_json, agent_summary=excluded.agent_summary,
                 candidates_json=excluded.candidates_json, chat_json=excluded.chat_json,
                 updated_at=excluded.updated_at""",
            (t.id, t.title, t.status.value, t.destination.label, t.destination.lat, t.destination.lon,
             t.origin.label, t.origin.lat, t.origin.lon, t.origin_mode.value,
             t.start_date.isoformat(), t.end_date.isoformat(), t.daily_start_time, t.daily_end_time,
             t.transport_mode.value, t.pace.value, json.dumps([i.value for i in t.interests]),
             t.constraints.model_dump_json(), t.agent_summary, _dump(t.candidates), _dump(t.chat),
             t.created_at, t.updated_at),
        )
        for d in t.days:
            conn.execute(
                """INSERT INTO itinerary_days (id, trip_id, day_number, trip_date, start_location_label,
                     summary, issues_json) VALUES (?,?,?,?,?,?,?)""",
                (d.id, t.id, d.day_number, d.trip_date.isoformat(), d.start_location_label,
                 d.summary, _dump(d.issues)),
            )
            for s in d.stops:
                conn.execute(
                    """INSERT INTO stops (id, itinerary_day_id, sequence, name, category, osm_type, osm_id,
                         lat, lon, address, tags_json, planned_arrival, planned_departure, visit_minutes,
                         earliest_start, user_note, reason, source_url, source_fetched_at, validation_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (s.id, d.id, s.sequence, s.name, s.category, s.osm_type, s.osm_id, s.lat, s.lon,
                     s.address, json.dumps(s.tags), s.planned_arrival, s.planned_departure,
                     s.visit_minutes, s.earliest_start, s.user_note, s.reason, s.source_url,
                     s.source_fetched_at, _dump(s.issues)),
                )
            for g in d.legs:
                conn.execute(
                    """INSERT INTO route_legs (id, itinerary_day_id, sequence, from_lat, from_lon, to_lat,
                         to_lon, mode, provider, status, distance_meters, duration_seconds,
                         geometry_json, fetched_at, error_code)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (g.id, d.id, g.sequence, g.from_lat, g.from_lon, g.to_lat, g.to_lon, g.mode.value,
                     g.provider, g.status.value, g.distance_meters, g.duration_seconds,
                     json.dumps(g.geometry) if g.geometry else None, g.fetched_at, g.error_code),
                )

    def delete_trip(self, trip_id: str) -> bool:
        try:
            with closing(self._connect()) as conn, conn:
                cur = conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
                return cur.rowcount > 0
        except sqlite3.Error:
            raise AppError("storage_error", "Could not delete the trip. Please retry.",
                           retryable=True, http_status=500)

    def purge_old_drafts(self, days: int = 7) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM trips WHERE status IN ('draft','generated') AND updated_at < ?", (cutoff,))
            return cur.rowcount

    # ------------------------------------------------------------ reads

    def list_trips(self, status: Optional[TripStatus] = TripStatus.saved) -> list[TripSummary]:
        """Trips with the given status (None = every trip), newest first, with booking info."""
        where, args = ("WHERE t.status = ?", (status.value,)) if status else ("", ())
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT t.id, t.title, t.status, t.destination_label, t.start_date, t.end_date,
                      t.updated_at, b.booking_ref, b.status AS booking_status
                    FROM trips t LEFT JOIN bookings b ON b.trip_id = t.id
                    {where} ORDER BY t.updated_at DESC""", args).fetchall()
        return [TripSummary(**dict(r)) for r in rows]

    def get_trip(self, trip_id: str) -> Optional[Trip]:
        with closing(self._connect()) as conn:
            t = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
            if t is None:
                return None
            day_rows = conn.execute(
                "SELECT * FROM itinerary_days WHERE trip_id = ? ORDER BY day_number", (trip_id,)).fetchall()
            days = []
            for d in day_rows:
                stops = conn.execute(
                    "SELECT * FROM stops WHERE itinerary_day_id = ? ORDER BY sequence", (d["id"],)).fetchall()
                legs = conn.execute(
                    "SELECT * FROM route_legs WHERE itinerary_day_id = ? ORDER BY sequence", (d["id"],)).fetchall()
                days.append(Day(
                    id=d["id"], day_number=d["day_number"], trip_date=d["trip_date"],
                    start_location_label=d["start_location_label"], summary=d["summary"],
                    issues=[Issue(**i) for i in json.loads(d["issues_json"])],
                    stops=[_row_to_stop(s) for s in stops],
                    legs=[_row_to_leg(g) for g in legs],
                ))
            b = conn.execute("SELECT * FROM bookings WHERE trip_id = ?", (trip_id,)).fetchone()
        return Trip(
            id=t["id"], title=t["title"], status=t["status"],
            destination=Location(label=t["destination_label"], lat=t["destination_lat"], lon=t["destination_lon"]),
            origin=Location(label=t["origin_label"], lat=t["origin_lat"], lon=t["origin_lon"]),
            origin_mode=t["origin_mode"], start_date=t["start_date"], end_date=t["end_date"],
            daily_start_time=t["daily_start_time"], daily_end_time=t["daily_end_time"],
            transport_mode=t["transport_mode"], pace=t["pace"],
            interests=json.loads(t["interests_json"]),
            constraints=TripConstraints.model_validate_json(t["constraints_json"]),
            agent_summary=t["agent_summary"],
            candidates=[Poi(**p) for p in json.loads(t["candidates_json"])],
            chat=[ChatMessage(**m) for m in json.loads(t["chat_json"])],
            booking=Booking(**dict(b)) if b else None,
            days=days, created_at=t["created_at"], updated_at=t["updated_at"],
        )

    # ------------------------------------------------------------ bookings

    def save_booking(self, b: Booking) -> None:
        cols = list(Booking.model_fields)
        values = [v.value if hasattr(v, "value") else (v.isoformat() if isinstance(v, date) else v)
                  for v in (getattr(b, c) for c in cols)]
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(f"INSERT OR REPLACE INTO bookings ({', '.join(cols)}) "
                             f"VALUES ({', '.join('?' * len(cols))})", values)
        except sqlite3.Error:
            raise AppError("storage_error", "Could not save the booking. Please retry.",
                           retryable=True, http_status=500)

    def get_booking(self, trip_id: str) -> Optional[Booking]:
        with closing(self._connect()) as conn:
            r = conn.execute("SELECT * FROM bookings WHERE trip_id = ?", (trip_id,)).fetchone()
        return Booking(**dict(r)) if r else None

    def get_booking_by_ref(self, booking_ref: str) -> Optional[Booking]:
        with closing(self._connect()) as conn:
            r = conn.execute("SELECT * FROM bookings WHERE booking_ref = ? COLLATE NOCASE",
                             (booking_ref.strip(),)).fetchone()
        return Booking(**dict(r)) if r else None

    def list_bookings(self) -> list[Booking]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM bookings ORDER BY booked_at DESC").fetchall()
        return [Booking(**dict(r)) for r in rows]

    def cancel_booking(self, b: Booking, action_id: str) -> bool:
        """Mark a booked trip cancelled and use up its confirmation, in one transaction.

        Returns False (and changes nothing) if the booking was no longer 'booked'.
        """
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """UPDATE bookings SET status = 'cancelled', cancelled_at = ?, cancellation_ref = ?,
                     cancellation_reason = ?, charge_percent = ?, refund_percent = ?, refund_status = ?,
                     refund_expected_by = ?
                   WHERE trip_id = ? AND status = 'booked'""",
                (b.cancelled_at, b.cancellation_ref, b.cancellation_reason, b.charge_percent,
                 b.refund_percent, b.refund_status,
                 b.refund_expected_by.isoformat() if b.refund_expected_by else None, b.trip_id))
            if cur.rowcount:
                conn.execute("UPDATE pending_actions SET used_at = ? WHERE id = ?", (b.cancelled_at, action_id))
            return cur.rowcount > 0

    # ------------------------------------------------------------ pending confirmations

    def save_pending(self, action_id: str, kind: str, trip_id: str, payload: dict,
                     created_at: str, expires_at: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO pending_actions (id, kind, trip_id, payload_json, created_at, expires_at) "
                         "VALUES (?,?,?,?,?,?)",
                         (action_id, kind, trip_id, json.dumps(payload), created_at, expires_at))

    def get_pending(self, action_id: str) -> Optional[dict]:
        with closing(self._connect()) as conn:
            r = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
        if r is None:
            return None
        return {**dict(r), "payload": json.loads(r["payload_json"])}

    # ------------------------------------------------------------ support chat + traces

    def add_support_message(self, role: str, content: str, meta: Optional[dict] = None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO support_messages (role, content, meta_json, at) VALUES (?,?,?,?)",
                         (role, content, json.dumps(meta or {}), datetime.now(timezone.utc).isoformat(timespec="seconds")))

    def list_support_messages(self, limit: int = 100) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM support_messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"role": r["role"], "content": r["content"], "meta": json.loads(r["meta_json"]), "at": r["at"]}
                for r in reversed(rows)]

    def clear_support_messages(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM support_messages")

    def save_trace(self, trace_id: str, at: str, user_message: str, summary: dict, events: list[dict]) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO assistant_traces (id, at, user_message, summary_json, events_json) "
                         "VALUES (?,?,?,?,?)",
                         (trace_id, at, user_message, json.dumps(summary), json.dumps(events)))

    def list_traces(self, limit: int = 20) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM assistant_traces ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["id"], "at": r["at"], "user_message": r["user_message"],
                 "summary": json.loads(r["summary_json"]), "events": json.loads(r["events_json"])}
                for r in rows]


def _row_to_stop(r: sqlite3.Row) -> Stop:
    return Stop(
        id=r["id"], sequence=r["sequence"], name=r["name"], category=r["category"],
        osm_type=r["osm_type"], osm_id=r["osm_id"], lat=r["lat"], lon=r["lon"], address=r["address"],
        tags=json.loads(r["tags_json"]), planned_arrival=r["planned_arrival"],
        planned_departure=r["planned_departure"], visit_minutes=r["visit_minutes"] or 60,
        earliest_start=r["earliest_start"],
        user_note=r["user_note"], reason=r["reason"], source_url=r["source_url"],
        source_fetched_at=r["source_fetched_at"],
        issues=[Issue(**i) for i in json.loads(r["validation_json"])],
    )


def _row_to_leg(r: sqlite3.Row) -> Leg:
    return Leg(
        id=r["id"], sequence=r["sequence"], from_lat=r["from_lat"], from_lon=r["from_lon"],
        to_lat=r["to_lat"], to_lon=r["to_lon"], mode=r["mode"], provider=r["provider"],
        status=r["status"], distance_meters=r["distance_meters"], duration_seconds=r["duration_seconds"],
        geometry=json.loads(r["geometry_json"]) if r["geometry_json"] else None,
        fetched_at=r["fetched_at"], error_code=r["error_code"],
    )
