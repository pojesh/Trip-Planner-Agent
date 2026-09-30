"""PyDeck map builders: planner preview and the trip map.

Note: pydeck treats plain strings as JavaScript expressions, so constant string
props must be quoted, e.g. radius_units="'pixels'" (not "pixels").
"""

import math
from typing import Optional

import pydeck as pdk

from ui.styles import day_rgb

START_RGB = [17, 24, 39]
FALLBACK_RGB = [245, 158, 11]
AREA_RGB = [99, 102, 241]


def _view(points: list[tuple[float, float]], focus: Optional[tuple[float, float]] = None) -> pdk.ViewState:
    """Fit the camera to (lat, lon) points, or centre on `focus`."""
    if focus:
        return pdk.ViewState(latitude=focus[0], longitude=focus[1], zoom=15, pitch=0)
    if not points:
        return pdk.ViewState(latitude=20, longitude=0, zoom=1.5)
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    span = max(max(lats) - min(lats), (max(lons) - min(lons)) * 0.75, 0.004)
    zoom = max(3.0, min(15.5, math.log2(360 / span) - 1.6))
    return pdk.ViewState(latitude=(min(lats) + max(lats)) / 2, longitude=(min(lons) + max(lons)) / 2,
                         zoom=zoom, pitch=0)


def _dashes(a: list[float], b: list[float], n: int = 24) -> list[dict]:
    """Split a straight [lon, lat] segment into short dashes (no deck.gl extension needed)."""
    out = []
    for i in range(0, n, 2):
        t0, t1 = i / n, (i + 1) / n
        out.append({"path": [[a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0],
                             [a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1]]})
    return out


def _start_layers(lat: float, lon: float, label: str = "Start") -> list[pdk.Layer]:
    data = [{"lat": lat, "lon": lon, "label": label}]
    return [
        pdk.Layer("ScatterplotLayer", id="start", data=data, get_position="[lon, lat]",
                  get_radius=12, radius_units="'pixels'", get_fill_color=START_RGB,
                  stroked=True, get_line_color=[255, 255, 255], line_width_units="'pixels'", get_line_width=3),
        pdk.Layer("TextLayer", id="start_label", data=data, get_position="[lon, lat]", get_text="label",
                  get_size=13, get_color=START_RGB, get_pixel_offset=[0, -24], font_weight=800),
    ]


def _area_layer(lat: float, lon: float, radius_m: float) -> pdk.Layer:
    return pdk.Layer("ScatterplotLayer", id="area", data=[{"lat": lat, "lon": lon}],
                     get_position="[lon, lat]", get_radius=radius_m, radius_units="'meters'",
                     get_fill_color=AREA_RGB + [16], stroked=True, get_line_color=AREA_RGB + [90],
                     line_width_units="'pixels'", get_line_width=1.5)


def _deck(layers, view, tooltip=None) -> pdk.Deck:
    return pdk.Deck(layers=layers, initial_view_state=view, map_provider="carto",
                    map_style=pdk.map_styles.CARTO_LIGHT, tooltip=tooltip)


def preview_deck(dest: Optional[dict], origin: Optional[dict], radius_m: float = 5000) -> pdk.Deck:
    layers, pts = [], []
    if dest:
        layers.append(_area_layer(dest["lat"], dest["lon"], radius_m))
        pts += [(dest["lat"] + d, dest["lon"] + d) for d in (-0.04, 0.04)]
    if origin:
        layers += _start_layers(origin["lat"], origin["lon"])
        pts.append((origin["lat"], origin["lon"]))
    return _deck(layers, _view(pts))


def _thin(path: list, max_points: int = 120) -> list:
    """Keep long route geometries light so the map stays smooth."""
    if len(path) <= max_points:
        return path
    step = len(path) / (max_points - 1)
    return [path[int(i * step)] for i in range(max_points - 1)] + [path[-1]]


def trip_deck(trip: dict, selected_day: int, selected_stop: Optional[str],
              focus: Optional[tuple[float, float]] = None) -> pdk.Deck:
    """Map of the selected day only: start point, numbered stops and the route between them."""
    origin = trip["origin"]
    day = next((d for d in trip["days"] if d["day_number"] == selected_day), trip["days"][0])
    n = day["day_number"]
    color = day_rgb(n)
    routes, dashes, stops, halo = [], [], [], []
    view_pts = [(origin["lat"], origin["lon"])]

    for leg in day["legs"]:
        geom = leg.get("geometry")
        if not geom:
            continue
        if leg["status"] == "routed":
            routes.append({"path": _thin(geom), "color": color + [230]})
        else:
            dashes += [{**d, "color": FALLBACK_RGB + [230]} for d in _dashes(geom[0], geom[-1])]
    for s in day["stops"]:
        stops.append({
            "id": s["id"], "day": n, "lat": s["lat"], "lon": s["lon"], "num": str(s["sequence"]),
            "name": s["name"],
            "info": f"{s.get('planned_arrival') or ''}–{s.get('planned_departure') or ''} · {s.get('category') or ''}",
        })
        view_pts.append((s["lat"], s["lon"]))
        if s["id"] == selected_stop:
            halo.append({"lat": s["lat"], "lon": s["lon"]})

    layers = [
        pdk.Layer("PathLayer", id="routes", data=routes, get_path="path", get_color="color", get_width=5,
                  width_units="'pixels'", cap_rounded=True, joint_rounded=True),
        pdk.Layer("PathLayer", id="fallback", data=dashes, get_path="path", get_color="color",
                  get_width=4, width_units="'pixels'", cap_rounded=True),
        pdk.Layer("ScatterplotLayer", id="halo", data=halo, get_position="[lon, lat]", get_radius=24,
                  radius_units="'pixels'", get_fill_color=color + [70]),
        *_start_layers(origin["lat"], origin["lon"]),
        pdk.Layer("ScatterplotLayer", id="stops", data=stops, get_position="[lon, lat]",
                  get_radius=13, radius_units="'pixels'", get_fill_color=color, stroked=True,
                  get_line_color=[255, 255, 255], line_width_units="'pixels'", get_line_width=2,
                  pickable=True, auto_highlight=True),
        pdk.Layer("TextLayer", id="numbers", data=stops, get_position="[lon, lat]", get_text="num",
                  get_size=12, get_color=[255, 255, 255], font_weight=800),
    ]
    tooltip = {"html": "<b>{name}</b><br/><span style='opacity:.8'>{info}</span>",
               "style": {"backgroundColor": "#111827", "color": "white", "fontSize": "12px",
                         "borderRadius": "10px", "padding": "8px 10px"}}
    return _deck(layers, _view(view_pts, focus), tooltip)
