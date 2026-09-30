"""Export helpers: calendar (.ics), JSON, and Google Maps directions links."""

import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode


def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _dt(day_iso: str, hhmm: str, base_minutes: int) -> str:
    """Floating local time (no timezone) — correct wherever the traveller is."""
    h, m = map(int, hhmm.split(":"))
    extra_days = 1 if h * 60 + m < base_minutes else 0  # wrapped past midnight
    d = date.fromisoformat(day_iso) + timedelta(days=extra_days)
    return f"{d.strftime('%Y%m%d')}T{h:02d}{m:02d}00"


def to_ics(trip: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Wayfinder Trip Planner//EN", "CALSCALE:GREGORIAN"]
    for day in trip["days"]:
        base = None
        for s in day["stops"]:
            if not s.get("planned_arrival") or not s.get("planned_departure"):
                continue
            h, m = map(int, s["planned_arrival"].split(":"))
            base = base if base is not None else h * 60 + m
            desc = [s.get("category") or "", s.get("source_url") or ""]
            if s.get("user_note"):
                desc.append(f"Note: {s['user_note']}")
            desc.append("Opening hours not verified — check before you go.")
            location = s.get("address") or f"{s['lat']:.5f}, {s['lon']:.5f}"
            lines += [
                "BEGIN:VEVENT",
                f"UID:{s['id']}@wayfinder",
                f"DTSTAMP:{stamp}",
                f"DTSTART:{_dt(day['trip_date'], s['planned_arrival'], base)}",
                f"DTEND:{_dt(day['trip_date'], s['planned_departure'], base)}",
                f"SUMMARY:{_ics_escape(s['name'])}",
                f"LOCATION:{_ics_escape(location)}",
                f"GEO:{s['lat']:.6f};{s['lon']:.6f}",
                "DESCRIPTION:" + _ics_escape("\n".join(d for d in desc if d)),
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def to_json(trip: dict) -> str:
    slim = {k: v for k, v in trip.items() if k not in ("candidates", "chat")}
    return json.dumps(slim, indent=2, ensure_ascii=False)


def google_maps_url(trip: dict, day: dict) -> str | None:
    stops = day["stops"]
    if not stops:
        return None
    o = trip["origin"]
    origin = f"{o['lat']:.6f},{o['lon']:.6f}"
    pts = [f"{s['lat']:.6f},{s['lon']:.6f}" for s in stops]
    back = trip["constraints"].get("return_to_start", True)
    destination = origin if back else pts[-1]
    waypoints = pts if back else pts[:-1]
    params = {"api": 1, "origin": origin, "destination": destination,
              "travelmode": "walking" if trip["transport_mode"] == "walking" else "driving"}
    if waypoints:
        params["waypoints"] = "|".join(waypoints[:9])  # Google Maps URLs accept up to 9 waypoints
    return "https://www.google.com/maps/dir/?" + urlencode(params)
