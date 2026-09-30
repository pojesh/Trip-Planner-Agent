
```markdown
# Trip Planner — Implementation Guide

## 1. Overview

A web app that turns a traveller's request into an editable, map-based, day-by-day
itinerary using only verifiable open geographic data. The model (Gemini) selects
and sequences real places; it never invents locations, hours, or travel times.

**Stack**
- **Frontend:** Streamlit (wide layout, PyDeck map), calling the backend over HTTP only.
- **Backend:** FastAPI — owns all business logic, external API calls, validation, and persistence.
- **Agent:** Google Gemini (structured JSON output) — selects/orders candidate places; never sourced facts itself.
- **Geocoding:** Nominatim (OpenStreetMap).
- **Places of interest:** Overpass API (OpenStreetMap).
- **Routing:** OSRM (public instance, swappable adapter).
- **Persistence:** SQLite (single-user local MVP).
- **Deferred:** Wikivoyage RAG, multi-user/Postgres deployment.

## 2. Problem & Success Definition

Turning vague preferences ("food, history, not rushed") into a geographically
coherent, time-boxed itinerary is hard to do without hallucinating places or
ignoring travel time. This MVP guarantees every stop traces back to real OSM
data and every travel time either comes from a routing provider or is honestly
labeled as unavailable.

**Success:** a user can create, inspect, edit, save, reopen, and delete a trip
plan with no account. A valid plan has a resolved destination, origin, dated
day windows, at least one sourced stop, and a route/route-status for every leg.

## 3. Scope

**In scope**
- One destination (city/metro area) per plan, 1–7 days (optimize for 1–3).
- One explicit origin: search result, or map pin/coordinates (current-location is stretch/deferred).
- Interest- and pace-aware POI discovery via Overpass.
- Gemini-assisted stop selection/ordering, constrained to sourced candidates.
- Route computation via OSRM where supported, with honest fallback when not.
- Map-first visualization with numbered stops, ordered route lines, hover/click sync.
- Manual reorder/remove/replace, notes, save/reopen via SQLite.

**Out of scope**
- Flights, hotels, bookings, prices, payments, availability guarantees.
- Multi-city trip optimization, collaboration, auth, notifications, mobile-native UX.
- Any claim that a place is open/safe/accessible without structured supporting data.

## 4. Architecture

```text
┌─────────────────────┐        HTTP/JSON        ┌──────────────────────────────┐
│   Streamlit UI       │ ───────────────────────▶│        FastAPI backend        │
│  - forms/session      │◀─────────────────────── │  - routers (trips, geocode)   │
│  - PyDeck map         │                          │  - PlanService (orchestrator) │
│  - itinerary panel     │                          │  - GeminiAgent (selection)    │
└─────────────────────┘                          │  - Nominatim client           │
                                                    │  - Overpass client            │
                                                    │  - Routing client (OSRM)      │
                                                    │  - Validator                  │
                                                    │  - SQLite repository          │
                                                    └──────────────────────────────┘
```

Rules:
- Streamlit **never** calls Nominatim/Overpass/OSRM/Gemini directly — only the FastAPI backend does.
- The backend is the sole source of truth for validation and persistence.
- All external clients sit behind adapters so providers are swappable.

### Repository layout

```text
trip-planner/
  backend/
    main.py                     # FastAPI app entry
    config.py
    routers/
      geocode.py
      trips.py
    domain/models.py            # Pydantic request/response/domain models
    services/
      plan_service.py           # orchestrates generation pipeline
      gemini_agent.py           # structured itinerary proposal
      validator.py
    clients/
      nominatim.py
      overpass.py
      routing.py
    repositories/
      sqlite_repo.py
    tests/
  frontend/
    app.py                      # Streamlit entry point
    api_client.py                # thin HTTP client to FastAPI
    ui/
      forms.py
      map.py
      itinerary.py
  requirements.txt
  .env.example
  README.md
```

## 5. Configuration

```env
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
NOMINATIM_BASE_URL=https://nominatim.openstreetmap.org
OVERPASS_BASE_URL=https://overpass-api.de/api/interpreter
ROUTING_BASE_URL=https://router.project-osrm.org
HTTP_USER_AGENT=trip-planner-mvp/1.0 (contact@example.com)
SQLITE_PATH=data/trip_planner.db
BACKEND_BASE_URL=http://localhost:8000   # used by Streamlit client
```

Validate all config at startup (both apps); fail fast with actionable messages; never log or display secrets.

## 6. Data Model (SQLite)

```sql
CREATE TABLE trips (
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
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE itinerary_days (
  id TEXT PRIMARY KEY,
  trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  day_number INTEGER NOT NULL,
  trip_date TEXT NOT NULL,
  start_location_label TEXT,
  UNIQUE(trip_id, day_number)
);

CREATE TABLE stops (
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
  user_note TEXT,
  source_url TEXT,
  source_fetched_at TEXT,
  validation_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(itinerary_day_id, sequence)
);

CREATE TABLE route_legs (
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

CREATE INDEX idx_days_trip ON itinerary_days(trip_id);
CREATE INDEX idx_stops_day ON stops(itinerary_day_id, sequence);
CREATE INDEX idx_legs_day ON route_legs(itinerary_day_id, sequence);
```

Enable `PRAGMA foreign_keys = ON` on every connection. Timestamps: UTC ISO-8601.

## 7. FastAPI Backend — API Surface

| Method & Path | Purpose |
|---|---|
| `GET /api/health` | Config/health check |
| `POST /api/geocode/search` | `{query, limit}` → Nominatim results (used for both origin & destination search) |
| `POST /api/trips/generate` | Body: preferences (origin, destination, dates, interests, pace, transport_mode, constraints) → runs full pipeline, returns an **unsaved draft** trip (days/stops/legs/warnings) |
| `GET /api/trips` | List saved trips (id, title, destination, dates, status) |
| `GET /api/trips/{trip_id}` | Full trip detail |
| `POST /api/trips/{trip_id}/save` | Persist a generated/edited draft |
| `PATCH /api/trips/{trip_id}/days/{day_id}/stops/{stop_id}` | Edit/remove/reorder a stop → recomputes affected legs only |
| `POST /api/trips/{trip_id}/days/{day_id}/recalculate` | Explicit recalculation trigger |
| `DELETE /api/trips/{trip_id}` | Delete a saved trip |

Streamlit's `api_client.py` wraps these; the UI never builds Overpass/Gemini requests itself.

## 8. External Service Contracts

### 8.1 Nominatim (`clients/nominatim.py`)
`search_place(query, limit=5) -> PlaceResult[]`
- Called only on explicit submit (never on keystroke).
- `GET /search` with `q`, `format=jsonv2`, `addressdetails=1`, `limit<=5`.
- Meaningful `User-Agent`; handle 429/403/timeout/malformed payloads.
- Session-cache normalized queries.
- Return: `label, lat, lon, type, class, osm_type, osm_id, boundingbox`. User must pick one — no auto-select.

### 8.2 Overpass (`clients/overpass.py`)
`find_pois(center_lat, center_lon, radius_m, categories, limit) -> Poi[]`
- Bounded query only (3–8 km radius by destination size), never unbounded.
- Interest → OSM tag mapping, e.g. culture → `tourism=museum|gallery|attraction`, outdoors → `leisure=park|nature_reserve`, landmarks → `historic=*|tourism=viewpoint`, food → `amenity=restaurant|cafe|fast_food`.
- Require name where possible; dedupe by `(osm_type, osm_id)`; drop bad coordinates.
- Cap results (e.g. 20/category, 80 total); timeout + one retry with jitter; cache per destination/category/radius.
- `Poi` fields: `osm_type, osm_id, name, lat, lon, category, address, opening_hours, website, wikidata, tags, source_url, fetched_at`.

### 8.3 Routing (`clients/routing.py`) — OSRM
`route(coordinates, mode) -> RouteResult`
- `GET /route/v1/{profile}/{lon,lat;...}` with `overview=full&geometries=geojson`.
- MVP profiles: `walking` (default), `driving` (optional). Unsupported modes are never silently presented as routed.
- Success → `status='routed'` + distance/duration/geometry/provider/fetched_at.
- Failure/no-route → `status='fallback'` (straight line, no fabricated duration) or `'unavailable'`; keep a safe `error_code`.
- Cache legs by rounded coordinate + mode within a session.

### 8.4 Gemini — itinerary proposal (`services/gemini_agent.py`)

Geocoding and POI discovery are performed by the backend **before** calling
Gemini — the model only makes the one decision that actually needs judgment:
**which candidates to use, in what order, with what visit durations.**

Use Gemini's **structured output mode** (`response_schema`) rather than a
multi-turn tool-calling loop — simpler and sufficient for this single decision point.

Input: user preferences + a compact list of candidate POIs (id, name, category, lat/lon, tags).
Output schema (validated with Pydantic):

```json
{
  "days": [{
    "day_number": 1,
    "stops": [
      {"candidate_id": "node/123", "visit_minutes": 90, "reason": "matches history interest"}
    ],
    "summary": "A compact, walkable first day"
  }],
  "assumptions": ["Opening hours were not verified"],
  "warnings": []
}
```

Guardrails:
- Reject unknown candidate IDs, duplicates in a day, stop-count/day-count violations, or invalid durations.
- On invalid output: send validation errors back once for a single correction attempt; if it still fails, fall back to deterministic greedy nearest-neighbor ordering.
- System instruction: act as a travel-planning coordinator, use only provided candidates, favor compact geographic clusters, preserve user constraints, never claim bookings/live availability/route data it wasn't given.
- The backend remains authoritative for final schedule feasibility — the model never self-validates.

## 9. Core UX Flow

1. Enter destination → **Search** → pick one of ≤5 Nominatim results (required, no free text).
2. Choose origin → search or map pin/coordinates.
3. Set dates, interests, pace, transport mode (default walking), optional notes.
4. **Generate** → staged progress: finding places → sequencing → calculating routes → finalizing.
5. Review on map (numbered markers, ordered route line) + day tabs with stop cards. Marker ↔ card selection is synced both ways.
6. Edit: remove/reorder/replace a stop → only affected legs recompute (or via explicit "Recalculate route").
7. Save → reopening restores inputs, stops, route snapshots, notes.

**Visual language:** distinct origin marker vs. subdued destination-area marker; numbered, day-colored stop markers; solid line = routed leg, dashed = fallback with "route unavailable" label; stop cards always show source/OSM link, last-fetched time, and any validation warnings. Every itinerary view states that hours/access/conditions should be verified before travel.

## 10. Planning Rules & Validation

Deterministic, backend-enforced (model output is never trusted blindly):

| Constraint | Rule |
|---|---|
| Daily span | Activities + routing must fit the selected daily window. |
| Stop count | relaxed 2–4, moderate 3–6, packed 5–8 (meals count if included). |
| Travel time | Use routed duration when available; never fabricate — mark leg as unavailable otherwise and exclude from "feasible" claims. |
| Geographic coherence | Flag/avoid legs over a max travel-time threshold; greedy nearest-next ordering as a baseline before routed refinement. |
| Opening hours | Treat missing/ambiguous data as unknown; never claim a place is open. |
| Duplicates | No repeated OSM element in a day; flag near-duplicate names within a small radius. |
| Origin per day | Day 1 starts at selected origin. Day 2+ starts at previous day's last stop only if that's a real overnight location; otherwise default back to origin and show that assumption explicitly. |

## 11. Generation Pipeline (`PlanService`)

1. **Validate inputs** — origin/destination resolved, valid dates, normalized interests/mode.
2. **Discover** — concurrent bounded Overpass queries per category, aggregate + dedupe.
3. **Pre-filter** — drop invalid candidates, attach source/freshness metadata.
4. **Plan** — Gemini structured call: candidate IDs + visit durations + day assignment only.
5. **Order & route** — start each day at its declared location, greedy-order if needed, call OSRM, derive arrival/departure times.
6. **Validate** — schedule overflow, duplicates, unsupported routes, long legs, unknown hours.
7. **Repair once** — on violations, one correction round-trip to Gemini with violation details; else deterministic fallback ordering.
8. **Return draft** — client renders map/panel with warnings; user may edit before saving.
9. **Edit** — recompute only the impacted legs/day, not the whole trip.

## 12. Resilience & Error Handling

| Concern | Behavior |
|---|---|
| Timeouts | Nominatim 6s, Overpass 12s (incl. one retry), OSRM 8s, Gemini 25s — configurable. |
| Rate limits | Respect `429`/`Retry-After`; exponential backoff with jitter, one retry only; no unbounded fan-out. |
| POI failure | Block generation with retry (can't source real places). |
| Route failure | Show dashed fallback leg + warning; itinerary stays usable. |
| Agent failure | One repair attempt, then deterministic nearest-neighbor fallback. |
| SQLite write failure | Roll back transaction; show unsaved-state banner; don't claim persistence succeeded. |
| Logging | Structured errors (`code`, safe `message`, retryable, provider); never log keys or full free-text queries. |

## 13. Testing Strategy

- **Unit:** date/pace validation, POI dedupe/caps, schedule validator, route-status mapping, SQLite CRUD + cascade.
- **Integration (mocked HTTP):** Nominatim submit-only behavior, Overpass fixture mapping, OSRM success/fail/timeout states, full pipeline (candidates → Gemini output → routing → saved plan).
- **API tests:** FastAPI routers via `TestClient` for each endpoint, including validation/error paths.
- **Manual smoke test:** plan a 2-day city trip end-to-end; edit a stop; save/reload; disable network after load to confirm saved plans still render.

## 14. Build Milestones

1. **Scaffold** — repo layout, FastAPI app skeleton + `/health`, Streamlit shell calling it, SQLite init, Pydantic domain models.
2. **Geocoding & map shell** — Nominatim endpoint/client, destination+origin selection UI, PyDeck map with origin/destination markers.
3. **Discovery & routing** — Overpass client + interest mapping, OSRM adapter, route layer with fallback styling.
4. **Agent & validation** — Gemini structured-output call, deterministic validator, repair-once logic.
5. **Persistence & editing** — save/load/delete endpoints & UI, stop edit/reorder with partial leg recompute.
6. **Polish & tests** — modern Streamlit styling/theme, unit/integration tests, manual smoke test, docs.

**Definition of done:** a user can search an origin/destination, generate a sourced multi-day itinerary with real routes (or honest fallbacks), edit it, save it, reload it, and trust that every stop and time shown is either provider-sourced or explicitly marked as an estimate/unavailable.

## 15. Deferred (Not in MVP)

- Browser current-location origin mode.
- Transit routing / live traffic.
- Wikivoyage RAG for contextual snippets.
- Multi-user deployment: PostgreSQL, auth, rate limiting, backups, observability.
- Production-grade geocoding/routing providers to replace public Nominatim/OSRM.
```