"""Planner view: destination + start point search, preferences, and generation."""

import math
from datetime import date, time, timedelta

import streamlit as st

import api_client as api
from api_client import ApiError
from ui.map import preview_deck
from ui.styles import esc, step_title

INTERESTS = {
    "culture": "🎨 Culture", "history": "🏛️ History", "landmarks": "📸 Landmarks",
    "outdoors": "🌳 Outdoors", "food": "🍽️ Food & cafés", "nightlife": "🍸 Nightlife",
    "shopping": "🛍️ Markets & shops", "religious": "⛪ Sacred sites",
}
PACES = {"relaxed": "🐢 Relaxed", "moderate": "🚶 Moderate", "packed": "⚡ Packed"}
MODES = {"walking": "🚶 Walking", "driving": "🚗 Driving"}
MAX_DAYS = 7


def _state():
    ss = st.session_state
    for key, default in [("dest", None), ("dest_results", None), ("origin", None), ("origin_results", None),
                         ("search_n", 0), ("gen_payload", None), ("pending_q", None)]:
        ss.setdefault(key, default)
    return ss


def _radius_m(bbox: list) -> float:
    """Same rule as the backend: 0.6 × half the bbox diagonal, clamped to 3–8 km."""
    if len(bbox or []) != 4:
        return 5000
    s, n, w, e = bbox
    dy = (n - s) * 111_000
    dx = (e - w) * 111_000 * math.cos(math.radians((n + s) / 2))
    return max(3000, min(8000, math.hypot(dx, dy) / 2 * 0.6))


def _result_label(r: dict) -> str:
    parts = [p.strip() for p in r["label"].split(",")]
    where = ", ".join(dict.fromkeys(parts[1:3] + ([r["country"]] if r.get("country") else [])))
    kind = (r.get("type") or r.get("place_class") or "").replace("_", " ")
    return f"{r['name']}  ·  {where}" + (f"  ({kind})" if kind else "")


def _picked(title: str, sub: str, icon: str, reset_key: str, on_reset) -> None:
    c1, c2 = st.columns([5, 1], vertical_alignment="center")
    c1.markdown(f'<div class="picked"><span style="font-size:1.3rem">{icon}</span>'
                f'<div><b>{esc(title)}</b><small>{esc(sub)}</small></div></div>', unsafe_allow_html=True)
    if c2.button("Change", key=reset_key, type="tertiary"):
        on_reset()
        st.rerun()


def _search_box(form_key: str, placeholder: str, results_key: str, near_bbox=None) -> None:
    ss = st.session_state
    with st.form(form_key, border=False):
        c1, c2 = st.columns([4, 1.3], vertical_alignment="bottom")
        query = c1.text_input("Search", placeholder=placeholder, label_visibility="collapsed")
        go = c2.form_submit_button("Search", type="primary", width="stretch")
    if go:  # the ONLY place a geocoding request is made
        if len(query.strip()) < 2:
            st.warning("Type at least 2 characters.")
            return
        with st.spinner("Searching OpenStreetMap…"):
            try:
                ss[results_key] = api.search_places(query.strip(), near_bbox)
                ss.search_n += 1
            except ApiError as e:
                st.error(e.message)
                return
        if not ss[results_key]:
            st.info("No matches found. Try another spelling or add the country, e.g. “Porto, Portugal”.")


def _pick_list(results_key: str, on_pick) -> None:
    ss = st.session_state
    results = ss.get(results_key)
    if not results:
        return
    with st.container(key=f"pick_{results_key}"):
        choice = st.radio("Choose the right match", list(range(len(results))), index=None,
                          format_func=lambda i: _result_label(results[i]),
                          key=f"radio_{results_key}_{ss.search_n}")
    if choice is not None:
        on_pick(results[choice])
        ss[results_key] = None
        st.rerun()


# ------------------------------------------------------------------ steps

def _destination_step(ss) -> None:
    step_title(1, "Where are you going?")
    if ss.dest:
        d = ss.dest
        _picked(d["name"], d["label"], "📍", "reset_dest",
                lambda: ss.update(dest=None, origin=None, pending_q=None))
        return
    _search_box("dest_form", "Search a city or area — e.g. Lisbon, Kyoto, Chicago", "dest_results")
    _pick_list("dest_results", lambda r: ss.update(dest=r, origin=None))


def _origin_step(ss) -> None:
    step_title(2, "Where does each day start?")
    if not ss.dest:
        st.caption("Pick a destination first.")
        return
    if ss.origin:
        o = ss.origin
        sub = "Your start point each day" if o["mode"] == "search" else f"{o['lat']:.5f}, {o['lon']:.5f}"
        _picked(o["label"].split(",")[0], sub, "🏨", "reset_origin", lambda: ss.update(origin=None))
        return
    st.caption("Your hotel, rental or a station. Days start here and (optionally) end here.")
    how = st.segmented_control("Start point", ["🔎 Search a place", "📌 Coordinates"],
                               default="🔎 Search a place", key="origin_how", label_visibility="collapsed")
    if how == "📌 Coordinates":
        c1, c2 = st.columns(2)
        lat = c1.number_input("Latitude", -90.0, 90.0, float(ss.dest["lat"]), format="%.6f")
        lon = c2.number_input("Longitude", -180.0, 180.0, float(ss.dest["lon"]), format="%.6f")
        label = st.text_input("Label", "My start point")
        if st.button("Use these coordinates"):
            ss.origin = {"label": label.strip() or "My start point", "lat": lat, "lon": lon, "mode": "pin"}
            st.rerun()
    else:
        _search_box("origin_form", f"Hotel, address or station in {ss.dest['name']}", "origin_results",
                    near_bbox=ss.dest.get("boundingbox") or None)
        _pick_list("origin_results", lambda r: ss.update(
            origin={"label": r["label"], "lat": r["lat"], "lon": r["lon"], "mode": "search"}))
    if st.button(f"Not sure yet — start from central {ss.dest['name']}", type="tertiary"):
        ss.origin = {"label": f"Central {ss.dest['name']}", "lat": ss.dest["lat"], "lon": ss.dest["lon"], "mode": "pin"}
        st.rerun()


def _preferences(ss) -> dict:
    step_title(3, "When?")
    today = date.today()
    dates = st.date_input("Trip dates", value=(today + timedelta(days=14), today + timedelta(days=15)),
                          min_value=today, format="DD/MM/YYYY")
    c1, c2 = st.columns(2)
    t_start = c1.time_input("Day starts", time(10, 0), step=1800)
    t_end = c2.time_input("Day ends", time(20, 0), step=1800)

    step_title(4, "What do you enjoy?")
    interests = st.pills("Interests", list(INTERESTS), selection_mode="multi", format_func=INTERESTS.get,
                         default=["culture", "landmarks", "food"], label_visibility="collapsed")
    # Stacked (not side-by-side columns) so the controls never overlap and spacing matches other fields.
    pace = st.segmented_control("Pace", list(PACES), format_func=PACES.get, default="moderate") or "moderate"
    mode = st.segmented_control("Getting around", list(MODES), format_func=MODES.get,
                                default="walking") or "walking"

    with st.expander("Must-sees, notes and more", icon=":material/edit_note:"):
        must = st.text_input("Must-see places (comma separated, up to 5)",
                             placeholder="e.g. Belém Tower, LX Factory")
        notes = st.text_area("Anything the planner should know?", max_chars=1000, height=80,
                             placeholder="Vegetarian, travelling with kids, avoid steep hills, budget-friendly…")
        back = st.toggle("Return to the start point at the end of each day", value=True)

    return {"dates": dates, "t_start": t_start, "t_end": t_end, "interests": interests or [],
            "pace": pace, "mode": mode, "must": must, "notes": notes, "back": back}


def _problems(ss, p: dict) -> list[str]:
    out = []
    if not ss.dest:
        out.append("choose a destination")
    if not ss.origin:
        out.append("set a start point")
    if not isinstance(p["dates"], (tuple, list)) or len(p["dates"]) != 2:
        out.append("pick a start and end date")
    elif (p["dates"][1] - p["dates"][0]).days + 1 > MAX_DAYS:
        out.append(f"keep the trip to {MAX_DAYS} days or fewer")
    if (p["t_end"].hour * 60 + p["t_end"].minute) - (p["t_start"].hour * 60 + p["t_start"].minute) < 120:
        out.append("make the day at least 2 hours long")
    if not p["interests"]:
        out.append("pick at least one interest")
    return out


def _payload(ss, p: dict) -> dict:
    return {
        "destination": ss.dest,
        "origin": {"label": ss.origin["label"], "lat": ss.origin["lat"], "lon": ss.origin["lon"]},
        "origin_mode": ss.origin["mode"],
        "start_date": p["dates"][0].isoformat(), "end_date": p["dates"][1].isoformat(),
        "daily_start_time": p["t_start"].strftime("%H:%M"), "daily_end_time": p["t_end"].strftime("%H:%M"),
        "interests": p["interests"], "pace": p["pace"], "transport_mode": p["mode"],
        "must_see": [m.strip() for m in p["must"].split(",") if m.strip()][:5],
        "notes": p["notes"].strip(), "return_to_start": p["back"],
        "clarifications": [], "allow_questions": True,
    }


# ------------------------------------------------------------------ generation

def run_generation(payload: dict) -> None:
    ss = st.session_state
    with st.status("Planning your trip…", expanded=True) as status:
        try:
            for ev in api.generate(payload):
                if ev["type"] == "stage":
                    status.update(label=ev["label"])
                    st.markdown(f"<span style='color:#4F46E5'>●</span> {esc(ev['label'])}", unsafe_allow_html=True)
                elif ev["type"] == "question":
                    ss.pending_q = ev
                    status.update(label="The agent has a quick question", state="complete", expanded=False)
                    st.rerun()
                elif ev["type"] == "result":
                    ss.trip = ev["trip"]
                    ss.selected_day, ss.selected_stop, ss.focus = 1, None, None
                    ss.pending_q, ss.gen_payload = None, None
                    status.update(label="Your trip is ready!", state="complete")
                    st.rerun()
                elif ev["type"] == "error":
                    status.update(label="Couldn't plan this trip", state="error")
                    st.error(ev["message"] + (" You can retry." if ev.get("retryable") else ""))
                    return
        except ApiError as e:
            status.update(label="Couldn't plan this trip", state="error")
            st.error(e.message)


def _answer(text: str | None) -> None:
    ss = st.session_state
    payload = ss.gen_payload
    q = ss.pending_q
    if text is None:
        payload["allow_questions"] = False
    else:
        payload["clarifications"].append({"question": q["question"], "answer": text})
    ss.pending_q = None
    run_generation(payload)


def _pending_question(ss) -> None:
    q = ss.pending_q
    if not q or not ss.gen_payload:
        return
    with st.chat_message("assistant", avatar="🧭"):
        st.markdown(q["question"])
    with st.container(horizontal=True):
        for i, r in enumerate(q.get("quick_replies") or []):
            if st.button(r, key=f"qr_{i}"):
                _answer(r)
        if st.button("Skip — just plan it", icon=":material/arrow_forward:", type="tertiary", key="qr_skip"):
            _answer(None)
    with st.form("answer_form", clear_on_submit=True, border=False):
        c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
        txt = c1.text_input("Your answer", placeholder="Type your answer…", label_visibility="collapsed")
        if c2.form_submit_button("Send", width="stretch") and txt.strip():
            _answer(txt.strip())


# ------------------------------------------------------------------ page

def render_planner() -> None:
    ss = _state()
    st.markdown('<div class="hero"><h1>Plan a trip that actually works.</h1>'
                '<p>Real places from OpenStreetMap, real walking &amp; driving times, and an AI agent '
                'that builds a day-by-day plan you can edit.</p></div>', unsafe_allow_html=True)
    left, right = st.columns([1.05, 1], gap="large")
    with left:
        with st.container(key="planner_card"):
            _destination_step(ss)
            _origin_step(ss)
            prefs = _preferences(ss)
            problems = _problems(ss, prefs)
            st.write("")
            if st.button("Plan my trip", icon=":material/auto_awesome:", type="primary", width="stretch", disabled=bool(problems)):
                ss.gen_payload = _payload(ss, prefs)
                ss.pending_q = None
                run_generation(ss.gen_payload)
            if problems:
                st.caption("To continue: " + ", ".join(problems) + ".")
            _pending_question(ss)
    with right:
        with st.container(key="preview_card"):
            radius = _radius_m(ss.dest.get("boundingbox")) if ss.dest else 5000
            st.pydeck_chart(preview_deck(ss.dest, ss.origin, radius), height=520, width="stretch")
            st.markdown(
                "<div class='legend'><span><i class='lg-dot' style='background:#111827'></i>Start point</span>"
                "<span><i class='lg-dot' style='background:rgba(99,102,241,.25)'></i>Area searched for places</span></div>",
                unsafe_allow_html=True)
        st.markdown(
            "<div class='disclaimer'>🔎 Places come from OpenStreetMap and routes from OSRM. "
            "The agent only chooses among real places — it never invents them. Opening hours and access "
            "aren't verified, so check before you go.</div>", unsafe_allow_html=True)
