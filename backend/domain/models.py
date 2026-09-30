"""Pydantic request/response/domain models shared by routers, services and repo."""

from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TRIP_DAYS = 7


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_hhmm(value: str) -> int:
    """'09:30' -> 570 minutes after midnight."""
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def fmt_hhmm(minutes: float) -> str:
    """Minutes after midnight -> 'HH:MM' (wraps past midnight)."""
    minutes = int(round(minutes)) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ---------------------------------------------------------------- enums

class Interest(str, Enum):
    culture = "culture"
    history = "history"
    landmarks = "landmarks"
    outdoors = "outdoors"
    food = "food"
    nightlife = "nightlife"
    shopping = "shopping"
    religious = "religious"


class Pace(str, Enum):
    relaxed = "relaxed"
    moderate = "moderate"
    packed = "packed"


# (min stops, max stops, target stops) per day
PACE_STOPS = {
    Pace.relaxed: (2, 4, 3),
    Pace.moderate: (3, 6, 5),
    Pace.packed: (5, 8, 7),
}


class TransportMode(str, Enum):
    walking = "walking"
    driving = "driving"


class OriginMode(str, Enum):
    search = "search"
    pin = "pin"


class TripStatus(str, Enum):
    draft = "draft"
    generated = "generated"
    saved = "saved"
    archived = "archived"


class LegStatus(str, Enum):
    routed = "routed"
    fallback = "fallback"
    unavailable = "unavailable"


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"


# ---------------------------------------------------------------- places

class PlaceResult(BaseModel):
    """A Nominatim search result the user can pick."""
    label: str
    name: str
    lat: float
    lon: float
    type: str = ""
    place_class: str = ""
    osm_type: str = ""
    osm_id: str = ""
    boundingbox: list[float] = Field(default_factory=list)  # [south, north, west, east]
    country: str = ""
    tags: dict[str, str] = Field(default_factory=dict)  # Nominatim extratags (hours, website…)


class Poi(BaseModel):
    """A sourced candidate place from Overpass."""
    id: str  # "node/123"
    osm_type: str
    osm_id: str
    name: str
    lat: float
    lon: float
    category: str
    interest: str
    address: Optional[str] = None
    opening_hours: Optional[str] = None
    website: Optional[str] = None
    wikidata: Optional[str] = None
    wikipedia: Optional[str] = None
    tags: dict[str, str] = Field(default_factory=dict)
    source_url: str
    fetched_at: str
    must_see: bool = False
    score: float = 0.0


class Location(BaseModel):
    label: str = Field(min_length=1, max_length=300)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


# ---------------------------------------------------------------- requests

class GeocodeRequest(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    limit: int = Field(default=5, ge=1, le=5)
    # Optional bias box [south, north, west, east], e.g. the chosen destination.
    near_bbox: Optional[list[float]] = None


class Clarification(BaseModel):
    question: str
    answer: str


class TripRequest(BaseModel):
    destination: PlaceResult
    origin: Location
    origin_mode: OriginMode = OriginMode.search
    start_date: date
    end_date: date
    daily_start_time: str = "10:00"
    daily_end_time: str = "20:00"
    interests: list[Interest] = Field(default_factory=list)
    pace: Pace = Pace.moderate
    transport_mode: TransportMode = TransportMode.walking
    must_see: list[str] = Field(default_factory=list, max_length=5)
    notes: str = Field(default="", max_length=1000)
    return_to_start: bool = True
    clarifications: list[Clarification] = Field(default_factory=list, max_length=4)
    allow_questions: bool = True

    @field_validator("daily_start_time", "daily_end_time")
    @classmethod
    def _time(cls, v: str) -> str:
        try:
            h, m = v.split(":")[:2]
            h, m = int(h), int(m)
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            raise ValueError("time must be HH:MM")
        return f"{h:02d}:{m:02d}"

    @field_validator("must_see")
    @classmethod
    def _must_see(cls, v: list[str]) -> list[str]:
        return [s.strip()[:80] for s in v if s.strip()]

    @model_validator(mode="after")
    def _check(self):
        if self.end_date < self.start_date:
            raise ValueError("end date must be on or after the start date")
        if self.day_count > MAX_TRIP_DAYS:
            raise ValueError(f"trips can be at most {MAX_TRIP_DAYS} days")
        if parse_hhmm(self.daily_end_time) - parse_hhmm(self.daily_start_time) < 120:
            raise ValueError("the daily window must be at least 2 hours (and end after it starts)")
        if not self.interests:
            self.interests = [Interest.landmarks, Interest.culture]
        return self

    @property
    def day_count(self) -> int:
        return (self.end_date - self.start_date).days + 1


class StopUpdate(BaseModel):
    note: Optional[str] = Field(default=None, max_length=1000)
    visit_minutes: Optional[int] = Field(default=None, ge=10, le=480)
    move_to: Optional[int] = Field(default=None, ge=1)  # new 1-based position
    replace_with: Optional[str] = None  # candidate id


class AddStopRequest(BaseModel):
    candidate_id: str


class SaveRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=120)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


# ---------------------------------------------------------------- itinerary

class Issue(BaseModel):
    code: str
    severity: Severity
    message: str
    stop_id: Optional[str] = None


class Stop(BaseModel):
    id: str
    sequence: int
    name: str
    category: Optional[str] = None
    osm_type: Optional[str] = None
    osm_id: Optional[str] = None
    lat: float
    lon: float
    address: Optional[str] = None
    tags: dict[str, str] = Field(default_factory=dict)
    planned_arrival: Optional[str] = None
    planned_departure: Optional[str] = None
    visit_minutes: int = 60
    earliest_start: Optional[str] = None  # "HH:MM": wait until then (e.g. dinner, sunset)
    user_note: Optional[str] = None
    reason: Optional[str] = None
    source_url: Optional[str] = None
    source_fetched_at: Optional[str] = None
    issues: list[Issue] = Field(default_factory=list)

    @property
    def candidate_id(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"


class Leg(BaseModel):
    id: str
    sequence: int  # 1-based; leg i ends at stop i (or back at start if i > stop count)
    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float
    mode: TransportMode
    provider: Optional[str] = None
    status: LegStatus
    distance_meters: Optional[float] = None
    duration_seconds: Optional[float] = None
    geometry: Optional[list[list[float]]] = None  # [[lon, lat], ...]
    fetched_at: Optional[str] = None
    error_code: Optional[str] = None


class Day(BaseModel):
    id: str
    day_number: int
    trip_date: date
    start_location_label: Optional[str] = None
    summary: Optional[str] = None
    stops: list[Stop] = Field(default_factory=list)
    legs: list[Leg] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class TripConstraints(BaseModel):
    """Snapshot of the less structured inputs and planning metadata."""
    must_see: list[str] = Field(default_factory=list)
    notes: str = ""
    return_to_start: bool = True
    search_radius_m: int = 5000
    destination_bbox: list[float] = Field(default_factory=list)
    planner: str = "gemini"  # "gemini" or "built-in"
    assumptions: list[str] = Field(default_factory=list)
    must_see_missing: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)  # computed trip-wide warnings


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str
    quick_replies: list[str] = Field(default_factory=list)
    at: str = Field(default_factory=utc_now)


class Trip(BaseModel):
    id: str
    title: str
    status: TripStatus
    destination: Location
    origin: Location
    origin_mode: OriginMode
    start_date: date
    end_date: date
    daily_start_time: str
    daily_end_time: str
    transport_mode: TransportMode
    pace: Pace
    interests: list[Interest]
    constraints: TripConstraints
    agent_summary: Optional[str] = None
    days: list[Day] = Field(default_factory=list)
    candidates: list[Poi] = Field(default_factory=list)
    chat: list[ChatMessage] = Field(default_factory=list)
    created_at: str
    updated_at: str


class TripSummary(BaseModel):
    id: str
    title: str
    status: TripStatus
    destination_label: str
    start_date: date
    end_date: date
    updated_at: str


# ---------------------------------------------------------------- agent plan (submit_itinerary args)

class AgentStop(BaseModel):
    candidate_id: str = Field(description="Exact id from find_places, e.g. 'node/123'")
    visit_minutes: int = Field(description="Time to spend there, 10-240")
    reason: str = Field(default="", description="Short reason this stop fits the traveller")
    earliest_start: Optional[str] = Field(
        default=None, description="Optional 'HH:MM': don't start before this time, e.g. '19:00' for dinner "
                                  "or '17:30' for a sunset spot. Leave empty to go straight on.")

    @field_validator("earliest_start")
    @classmethod
    def _hhmm(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        try:
            h, m = (int(x) for x in v.strip().split(":")[:2])
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            return None  # ignore a malformed time rather than failing the whole plan
        return f"{h:02d}:{m:02d}"


class AgentDay(BaseModel):
    day_number: int = Field(description="1-based day number")
    stops: list[AgentStop] = Field(description="Stops in visiting order")
    summary: str = Field(default="", description="One-line summary of the day")


class ItineraryPlan(BaseModel):
    days: list[AgentDay] = Field(description="Every day of the trip")
    summary: str = Field(description="Short friendly message to the traveller about the plan")
    assumptions: list[str] = Field(default_factory=list, description="Assumptions you made")
