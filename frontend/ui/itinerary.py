"""Trip view: header, map, day timeline with editable stop cards."""

from datetime import datetime
from urllib.parse import quote

import streamlit as st

import api_client as api
from api_client import ApiError
from ui.chat import render_chat
from ui.export import google_maps_url, to_ics, to_json
from ui.map import trip_deck
from ui.styles import day_color, esc, fmt_date, fmt_distance, fmt_minutes

MODE_ICON = {"walking": "🚶", "driving": "🚗"}


# ------------------------------------------------------------------ helpers

def _day(trip: dict, number: int) -> dict:
    return next((d for d in trip["days"] if d["day_number"] == number), trip["days"][0])


def _apply(result: dict, message: str) -> None:
    ss = st.session_state
    ss.trip = result["trip"]
    n = result.get("rerouted_legs", 0)
    ss.flash = message + (f" · {n} leg{'s' if n != 1 else ''} re-routed" if n else "")


def _call(fn, *args, message: str = "Updated", **kwargs) -> None:
    """Run an edit API call from a button callback and store the result/errors in state."""
    try:
        _apply(fn(*args, **kwargs), message)
    except ApiError as e:
        st.session_state.flash_error = e.message


def _haversine_km(a, b) -> float:
    from math import asin, cos, radians, sin, sqrt
    dlat, dlon = radians(b[0] - a[0]), radians(b[1] - a[1])
    h = sin(dlat / 2) ** 2 + cos(radians(a[0])) * cos(radians(b[0])) * sin(dlon / 2) ** 2
    return 12742 * asin(sqrt(h))


def _unused_candidates(trip: dict, near: tuple[float, float], limit: int = 12) -> list[dict]:
    used = {f"{s['osm_type']}/{s['osm_id']}" for d in trip["days"] for s in d["stops"]}
    pool = [c for c in trip["candidates"] if c["id"] not in used]
    pool.sort(key=lambda c: _haversine_km(near, (c["lat"], c["lon"])))
    return pool[:limit]


def _cand_label(c: dict, near) -> str:
    return f"{c['name']} · {c['category']} · {_haversine_km(near, (c['lat'], c['lon'])):.1f} km"


def _fetched(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %H:%M UTC")
    except ValueError:
        return ""


def _safe_url(url: str | None) -> str | None:
    return url if url and url.startswith(("http://", "https://")) else None


# ------------------------------------------------------------------ callbacks

def _select_day():
    ss = st.session_state
    if ss.get("day_pick"):
        ss.selected_day, ss.selected_stop, ss.focus = ss.day_pick, None, None


def _locate(stop: dict):
    ss = st.session_state
    ss.selected_stop, ss.focus = stop["id"], (stop["lat"], stop["lon"])


def _move(trip_id, day_id, stop_id, to):
    _call(api.update_stop, trip_id, day_id, stop_id, move_to=to, message="Stop moved")


def _remove(trip_id, day_id, stop_id, name):
    _call(api.remove_stop, trip_id, day_id, stop_id, message=f"Removed {name}")


def _save_note(trip_id, day_id, stop_id):
    ss = st.session_state
    _call(api.update_stop, trip_id, day_id, stop_id, note=ss[f"note_{stop_id}"],
          visit_minutes=int(ss[f"mins_{stop_id}"]), message="Stop updated")


def _replace(trip_id, day_id, stop_id):
    cid = st.session_state.get(f"repl_{stop_id}")
    if cid:
        _call(api.update_stop, trip_id, day_id, stop_id, replace_with=cid, message="Stop replaced")


def _add(trip_id, day_id):
    cid = st.session_state.get(f"add_{day_id}")
    if cid:
        _call(api.add_stop, trip_id, day_id, cid, message="Place added")


def _recalc(trip_id, day_id):
    _call(api.recalculate, trip_id, day_id, message="Routes recalculated")


def _save(trip_id):
    ss = st.session_state
    try:
        ss.trip = api.save_trip(trip_id, ss.get("save_title"))
        ss.flash = "Saved to My trips"
    except ApiError as e:
        ss.flash_error = f"Not saved: {e.message}"


# ------------------------------------------------------------------ header

def _header(trip: dict) -> None:
    ss = st.session_state
    saved = trip["status"] == "saved"
    badge = '<span class="badge badge-saved">SAVED</span>' if saved else '<span class="badge badge-draft">DRAFT</span>'
    planner = trip["constraints"].get("planner", "gemini")
    pbadge = ('<span class="badge badge-ai">AI agent</span>' if planner == "gemini"
              else '<span class="badge badge-ai">Built-in planner</span>')
    n_days = len(trip["days"])
    dates = fmt_date(trip["start_date"]) + ("" if n_days == 1 else f" – {fmt_date(trip['end_date'])}")
    c1, c2 = st.columns([2.1, 1.3], vertical_alignment="bottom")
    c1.markdown(
        f'<div class="trip-h1">{esc(trip["title"])}{badge}{pbadge}</div>'
        f'<div class="trip-sub">📍 {esc(trip["destination"]["label"].split(",")[0])} &nbsp;·&nbsp; 📅 {dates} '
        f'&nbsp;·&nbsp; {MODE_ICON[trip["transport_mode"]]} {trip["transport_mode"].capitalize()} '
        f'&nbsp;·&nbsp; {trip["pace"].capitalize()} pace &nbsp;·&nbsp; <span style="white-space:nowrap">🕙 {trip["daily_start_time"]}–{trip["daily_end_time"]}</span></div>',
        unsafe_allow_html=True)
    with c2, st.container(horizontal=True, horizontal_alignment="right"):
        if not saved:
            with st.popover("Save trip", icon=":material/bookmark_add:", type="primary"):
                st.text_input("Trip name", value=trip["title"], key="save_title", max_chars=120)
                st.button("Save", type="primary", on_click=_save, args=(trip["id"],), width="stretch")
        with st.popover("Export", icon=":material/download:"):
            slug = quote(trip["title"].replace(" ", "-").lower())
            st.download_button("Calendar (.ics)", to_ics(trip), f"{slug}.ics", "text/calendar", icon=":material/calendar_month:",
                               width="stretch")
            st.download_button("JSON", to_json(trip), f"{slug}.json", "application/json", icon=":material/data_object:",
                               width="stretch")
        url = google_maps_url(trip, _day(trip, ss.selected_day))
        if url:
            st.link_button("Open day in Google Maps", url, icon=":material/map:")

    note = ("Changes save automatically." if saved
            else "This is an unsaved draft — save it to keep it in My trips.")
    st.markdown(
        f"<div class='disclaimer'>ⓘ Draft itinerary built from OpenStreetMap data. Opening hours, access and "
        f"conditions are not verified — please check before you go. {note}</div>", unsafe_allow_html=True)
    for w in trip["constraints"].get("warnings", []):
        st.markdown(f"<div class='issue issue-warning'>⚠️ {esc(w)}</div>", unsafe_allow_html=True)


# ------------------------------------------------------------------ map

def _map(trip: dict) -> None:
    ss = st.session_state
    with st.container(key="map_card"):
        event = st.pydeck_chart(trip_deck(trip, ss.selected_day, ss.selected_stop, ss.focus),
                                height=640, width="stretch", on_select="rerun",
                                selection_mode="single-object", key="trip_map")
        colors = (f"<span><i class='lg-dot' style='background:{day_color(ss.selected_day)}'></i>"
                  f"Day {ss.selected_day} stops</span>")
        st.markdown(
            f"<div class='legend'><span><i class='lg-dot' style='background:#111827'></i>Start</span>{colors}"
            f"<span><i class='lg-line' style='border-top-color:{day_color(ss.selected_day)}'></i>Routed</span>"
            "<span><i class='lg-dash'></i>No route (straight line)</span></div>",
            unsafe_allow_html=True)
    picked = (event.selection.get("objects", {}) if event and event.selection else {}).get("stops") or []
    if picked:
        obj = picked[0]
        if obj.get("id") != ss.get("last_pick"):  # a new marker click
            ss.last_pick = obj["id"]
            ss.selected_day, ss.selected_stop, ss.focus = int(obj["day"]), obj["id"], None
            st.rerun()


# ------------------------------------------------------------------ timeline

def _leg_row(leg: dict, target: str) -> None:
    icon = MODE_ICON.get(leg["mode"], "➜")
    if leg["status"] == "routed":
        txt = f"{icon} {fmt_minutes(leg['duration_seconds'] / 60)} · {fmt_distance(leg['distance_meters'])} to {esc(target)}"
        st.markdown(f"<div class='leg'>{txt}</div>", unsafe_allow_html=True)
    else:
        dist = f"{fmt_distance(leg['distance_meters'])} straight line · " if leg.get("distance_meters") else ""
        st.markdown(f"<div class='leg leg-bad'>⚠️ No route found to {esc(target)} — {dist}travel time unknown</div>",
                    unsafe_allow_html=True)


def _stop_card(trip: dict, day: dict, stop: dict, i: int) -> None:
    ss = st.session_state
    tid, did, sid = trip["id"], day["id"], stop["id"]
    last = i == len(day["stops"]) - 1
    selected = sid == ss.selected_stop
    tags = stop.get("tags") or {}
    with st.container(key=("stopsel_" if selected else "stop_") + sid):
        hours = tags.get("opening_hours")
        links = [f'<a href="{esc(stop["source_url"])}" target="_blank">OpenStreetMap ↗</a>'] if stop.get("source_url") else []
        if _safe_url(tags.get("website") or tags.get("contact:website")):
            links.append(f'<a href="{esc(tags.get("website") or tags.get("contact:website"))}" target="_blank">Website ↗</a>')
        if ":" in (tags.get("wikipedia") or ""):
            lang, title = tags["wikipedia"].split(":", 1)
            links.append(f'<a href="https://{esc(lang)}.wikipedia.org/wiki/{quote(title)}" target="_blank">Wikipedia ↗</a>')
        body = []
        if stop.get("address"):
            body.append(f"📍 {esc(stop['address'])}")
        body.append(f"🕒 Listed hours: {esc(hours)} <span class='src'>(unverified)</span>" if hours
                    else "🕒 Opening hours unknown — check before going")
        if stop.get("reason"):
            body.append(f"<span class='reason'>“{esc(stop['reason'])}”</span>")
        if stop.get("user_note"):
            body.append(f"<span class='note'>📝 {esc(stop['user_note'])}</span>")
        body.append(" · ".join(links) + f" <span class='src'>· fetched {esc(_fetched(stop.get('source_fetched_at')))}</span>")
        issues = "".join(f"<div class='issue issue-{i_['severity']}'>{esc(i_['message'])}</div>"
                         for i_ in stop.get("issues", []) if i_["severity"] != "info")
        st.markdown(
            f"<div class='stop-head'><div class='num' style='background:{day_color(day['day_number'])}'>{stop['sequence']}</div>"
            f"<div><div class='stop-name'>{esc(stop['name'])}<span class='chip'>{esc(stop.get('category') or 'Place')}</span></div>"
            f"<div class='stop-time'>{stop.get('planned_arrival') or '—'} – {stop.get('planned_departure') or '—'}"
            f" · {fmt_minutes(stop['visit_minutes'])}"
            f"{' · planned for ' + stop['earliest_start'] + ' (free time before)' if stop.get('earliest_start') else ''}"
            f"</div></div></div>"
            f"<div class='stop-body'>{'<br/>'.join(body)}{issues}</div>",
            unsafe_allow_html=True)

        with st.container(horizontal=True, key=f"acts_{sid}", gap="small"):
            st.button(":material/arrow_upward:", key=f"up_{sid}", help="Move earlier", disabled=i == 0,
                      on_click=_move, args=(tid, did, sid, stop["sequence"] - 1))
            st.button(":material/arrow_downward:", key=f"dn_{sid}", help="Move later", disabled=last,
                      on_click=_move, args=(tid, did, sid, stop["sequence"] + 1))
            st.button(":material/my_location:", key=f"loc_{sid}", help="Show on map", on_click=_locate, args=(stop,))
            with st.popover(":material/edit_note:", help="Note & visit time"):
                st.text_area("Your note", value=stop.get("user_note") or "", key=f"note_{sid}", max_chars=1000)
                st.number_input("Visit time (minutes)", 10, 480, int(stop["visit_minutes"]), step=15, key=f"mins_{sid}")
                st.button("Save", key=f"savenote_{sid}", type="primary", on_click=_save_note, args=(tid, did, sid))
            with st.popover(":material/swap_horiz:", help="Replace with a nearby place"):
                near = (stop["lat"], stop["lon"])
                options = _unused_candidates(trip, near)
                if options:
                    labels = {c["id"]: _cand_label(c, near) for c in options}
                    st.radio("Nearby alternatives (from OpenStreetMap)", list(labels), format_func=labels.get,
                             key=f"repl_{sid}")
                    st.button("Replace", key=f"dorepl_{sid}", type="primary", on_click=_replace, args=(tid, did, sid))
                else:
                    st.caption("No other sourced places nearby. Ask the assistant to find more.")
            st.button(":material/delete:", key=f"rm_{sid}", help="Remove stop", on_click=_remove, args=(tid, did, sid, stop["name"]))


def _day_panel(trip: dict) -> None:
    ss = st.session_state
    days = trip["days"]
    ss.day_pick = ss.selected_day
    st.segmented_control("Day", [d["day_number"] for d in days], key="day_pick", on_change=_select_day,
                         format_func=lambda n: f"Day {n} · {fmt_date(_day(trip, n)['trip_date'])}",
                         label_visibility="collapsed")
    day = _day(trip, ss.selected_day)
    stops, legs = day["stops"], day["legs"]

    routed = [g for g in legs if g["status"] == "routed"]
    travel = sum(g["duration_seconds"] for g in routed) / 60
    dist = sum(g["distance_meters"] or 0 for g in routed)
    problems = [i for i in day["issues"] if i["severity"] != "info"] + \
               [i for s in stops for i in s["issues"] if i["severity"] != "info"]
    finish = stops[-1]["planned_departure"] if stops else "—"
    st.markdown(
        "<div class='stats'>"
        f"<div class='stat'><span>Stops</span><b>{len(stops)}</b></div>"
        f"<div class='stat'><span>Travel</span><b>{fmt_minutes(travel)}</b></div>"
        f"<div class='stat'><span>Distance</span><b>{fmt_distance(dist)}</b></div>"
        f"<div class='stat'><span>Last stop ends</span><b>{finish}</b></div>"
        f"<div class='stat'><span>Issues</span><b>{'✓ 0' if not problems else len(problems)}</b></div></div>",
        unsafe_allow_html=True)
    if day.get("summary"):
        st.markdown(f"<div class='day-summary'>{esc(day['summary'])}</div>", unsafe_allow_html=True)
    for issue in day["issues"]:
        if issue["code"] != "route_unavailable":  # shown inline on the leg instead
            st.markdown(f"<div class='issue issue-{issue['severity']}'>{esc(issue['message'])}</div>",
                        unsafe_allow_html=True)

    st.markdown(f"<div class='startpt'><span class='dot'>▶</span>Start · {esc(trip['origin']['label'].split(',')[0])}"
                f" · {trip['daily_start_time']}</div>", unsafe_allow_html=True)
    for i, stop in enumerate(stops):
        if i < len(legs):
            _leg_row(legs[i], stop["name"])
        _stop_card(trip, day, stop, i)
    if len(legs) > len(stops) and stops:
        _leg_row(legs[len(stops)], "your start point")
        st.markdown(f"<div class='startpt'><span class='dot'>■</span>Back at start</div>", unsafe_allow_html=True)
    if not stops:
        st.info("No stops on this day yet — add a place below or ask the assistant.")

    st.write("")
    with st.container(horizontal=True):
        with st.popover("Add a place", icon=":material/add_location_alt:"):
            ref = (stops[len(stops) // 2]["lat"], stops[len(stops) // 2]["lon"]) if stops else \
                (trip["origin"]["lat"], trip["origin"]["lon"])
            options = _unused_candidates(trip, ref, limit=40)
            if options:
                labels = {c["id"]: _cand_label(c, ref) for c in options}
                st.selectbox("Sourced places near this day", list(labels), format_func=labels.get, key=f"add_{day['id']}")
                st.caption("It's inserted where it adds the least detour.")
                st.button("Add to day", type="primary", on_click=_add, args=(trip["id"], day["id"]))
            else:
                st.caption("No unused places left. Ask the assistant to find more.")
        st.button("Recalculate route", icon=":material/refresh:", on_click=_recalc, args=(trip["id"], day["id"]),
                  help="Re-fetch every route for this day from the routing service")

    assumptions = trip["constraints"].get("assumptions") or []
    if assumptions:
        with st.expander("Assumptions the planner made"):
            for a in assumptions:
                st.markdown(f"- {esc(a)}", unsafe_allow_html=True)


# ------------------------------------------------------------------ page

def render_trip(agent_on: bool) -> None:
    ss = st.session_state
    trip = ss.trip
    ss.setdefault("selected_day", 1)
    ss.setdefault("selected_stop", None)
    ss.setdefault("focus", None)
    if ss.selected_day not in [d["day_number"] for d in trip["days"]]:
        ss.selected_day = trip["days"][0]["day_number"] if trip["days"] else 1

    _header(trip)
    if not trip["days"]:
        st.warning("This trip has no days.")
        return
    left, right = st.columns([1.25, 1], gap="large")
    with left:
        _map(trip)
    with right:
        with st.container(key="panel_card"):
            panel = st.segmented_control("View", ["Itinerary", "Assistant"], default="Itinerary",
                                         key="panel", label_visibility="collapsed") or "Itinerary"
            if panel == "Assistant":
                render_chat(trip, agent_on)
            else:
                _day_panel(trip)
