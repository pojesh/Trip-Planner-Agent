"""Global CSS and small HTML/format helpers shared by the UI modules."""

import html
from datetime import date

import streamlit as st

# Accessible, distinct day colours (hex, and RGB for the map).
DAY_COLORS = ["#4F46E5", "#EA580C", "#0D9488", "#DB2777", "#CA8A04", "#0284C7", "#7C3AED"]


def day_color(day_number: int) -> str:
    return DAY_COLORS[(day_number - 1) % len(DAY_COLORS)]


def day_rgb(day_number: int) -> list[int]:
    h = day_color(day_number).lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def esc(text) -> str:
    return html.escape(str(text or ""))


def fmt_minutes(minutes: float) -> str:
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def fmt_distance(meters: float) -> str:
    return f"{meters:.0f} m" if meters < 1000 else f"{meters / 1000:.1f} km"


def fmt_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%a %d %b").replace(" 0", " ")


CSS = """
<style>
/* Colours, fonts and radii live in .streamlit/config.toml; this only styles the cards/timeline. */
#MainMenu, footer, [data-testid="stDecoration"] { display: none !important; }
/* Date pickers open a calendar, so show a hand cursor rather than a text cursor. */
[data-testid="stDateInputField"], [data-testid="stDateInputField"] * { cursor: pointer !important; }
/* Hide the "Press Enter to submit form" hint (Enter still submits). */
[data-testid="InputInstructions"] { display: none !important; }
header[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 1.6rem; padding-bottom: 2rem; max-width: 1480px; }
h1, h2, h3, h4 { letter-spacing: -0.02em; }

/* ---------- sidebar ---------- */
.brand { display:flex; align-items:center; gap:.6rem; margin:.2rem 0 1rem; }
.brand-logo { width:38px; height:38px; border-radius:12px; display:grid; place-items:center;
  background: linear-gradient(135deg,#6366F1,#EC4899); color:#fff; font-size:20px;
  box-shadow: 0 6px 18px rgba(99,102,241,.35); }
.brand-name { font-weight:800; font-size:1.15rem; line-height:1.1; }
.brand-tag { font-size:.75rem; color:#6B7085; }
.side-label { font-size:.72rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
  color:#8A8FA3; margin:1.2rem 0 .4rem; }
[class*="st-key-tripitem_"] { background:#F7F8FC; border:1px solid #ECEEF5; border-radius:14px;
  padding:.55rem .7rem .45rem; margin-bottom:.45rem; }
.trip-title { font-weight:600; font-size:.92rem; }
.trip-meta { font-size:.75rem; color:#7A7F94; }
.status-dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }

/* ---------- hero + cards ---------- */
.hero h1 { font-size:2.3rem; font-weight:800; margin:0 0 .2rem; line-height:1.15;
  background: linear-gradient(90deg,#1E1B4B 0%,#4F46E5 55%,#DB2777 100%);
  -webkit-background-clip:text; background-clip:text; color:transparent; }
.hero p { color:#5C6178; margin:0 0 1.1rem; font-size:1.02rem; }
.st-key-planner_card, .st-key-preview_card, .st-key-map_card, .st-key-panel_card {
  background:#FFFFFF; border:1px solid #E7E9F2; border-radius:22px; padding:1.25rem 1.3rem;
  box-shadow: 0 1px 2px rgba(16,24,40,.04), 0 8px 24px rgba(16,24,40,.05); }
.st-key-map_card { padding:.6rem; }
.step { display:flex; align-items:center; gap:.55rem; font-weight:700; font-size:1rem; margin:.4rem 0 .5rem; }
.step-n { width:24px; height:24px; border-radius:50%; background:#EEF0FF; color:#4F46E5;
  display:grid; place-items:center; font-size:.78rem; font-weight:800; }
.picked { display:flex; align-items:center; gap:.6rem; background:#F1F5FF; border:1px solid #DCE3FF;
  border-radius:14px; padding:.6rem .8rem; }
.picked b { font-weight:650; }
.picked small { color:#6B7085; display:block; font-size:.78rem; }

/* radio results rendered as cards */
[class*="st-key-pick_"] div[role="radiogroup"] { gap:.4rem; }
[class*="st-key-pick_"] div[role="radiogroup"] > label { background:#FAFBFE; border:1px solid #E7E9F2;
  border-radius:12px; padding:.5rem .7rem; width:100%; margin:0; transition: all .15s ease; }
[class*="st-key-pick_"] div[role="radiogroup"] > label:hover { border-color:#A5B4FC; background:#F5F7FF; }

/* ---------- trip header ---------- */
.trip-h1 { font-size:1.75rem; font-weight:800; margin:0; line-height:1.2; }
.trip-sub { color:#646A80; font-size:.92rem; margin-top:.2rem; }
.badge { display:inline-block; font-size:.72rem; font-weight:700; padding:.18rem .55rem; border-radius:999px;
  vertical-align:middle; margin-left:.5rem; letter-spacing:.02em; }
.badge-draft { background:#FEF3C7; color:#92400E; }
.badge-saved { background:#DCFCE7; color:#166534; }
.badge-ai { background:#EEF0FF; color:#4338CA; }
.disclaimer { font-size:.8rem; color:#6B7085; background:#F8F9FC; border:1px dashed #D9DCE8;
  border-radius:12px; padding:.45rem .75rem; margin:.5rem 0 .2rem; }

/* ---------- day summary ---------- */
.stats { display:flex; flex-wrap:wrap; gap:.5rem; margin:.3rem 0 .6rem; }
.stat { background:#F7F8FC; border:1px solid #ECEEF5; border-radius:12px; padding:.4rem .7rem; min-width:78px; }
.stat b { display:block; font-size:1rem; font-weight:700; }
.stat span { font-size:.7rem; color:#7A7F94; text-transform:uppercase; letter-spacing:.05em; }
.day-summary { color:#4B5068; font-size:.9rem; margin:.1rem 0 .5rem; }
.issue { font-size:.83rem; border-radius:10px; padding:.4rem .65rem; margin:.25rem 0; }
.issue-error { background:#FEF2F2; color:#991B1B; border:1px solid #FECACA; }
.issue-warning { background:#FFFBEB; color:#92400E; border:1px solid #FDE68A; }
.issue-info { background:#F1F5F9; color:#475569; border:1px solid #E2E8F0; }

/* ---------- timeline ---------- */
.leg { margin:.15rem 0 .15rem 1.05rem; padding:.35rem 0 .35rem 1.1rem; border-left:2px solid #D6DAE8;
  font-size:.8rem; color:#6B7085; }
.leg-bad { border-left:2px dashed #F59E0B; color:#92400E; }
.startpt { display:flex; align-items:center; gap:.55rem; font-size:.85rem; color:#374151; font-weight:600; }
.startpt .dot { width:26px; height:26px; border-radius:50%; background:#111827; color:#fff;
  display:grid; place-items:center; font-size:.7rem; border:3px solid #fff; box-shadow:0 0 0 1px #D1D5DB; }
[class*="st-key-stop_"], [class*="st-key-stopsel_"] { background:#FFFFFF; border:1px solid #E7E9F2;
  border-radius:16px; padding:.75rem .85rem .55rem; box-shadow:0 1px 2px rgba(16,24,40,.04); gap:.35rem; }
[class*="st-key-stopsel_"] { border:2px solid #6366F1; box-shadow:0 0 0 4px rgba(99,102,241,.12); }
.stop-head { display:flex; gap:.7rem; align-items:flex-start; }
.num { flex:0 0 auto; width:28px; height:28px; border-radius:50%; color:#fff; font-weight:800; font-size:.82rem;
  display:grid; place-items:center; box-shadow:0 2px 6px rgba(0,0,0,.15); }
.stop-name { font-weight:700; font-size:.98rem; line-height:1.25; }
.stop-time { font-size:.8rem; color:#4F46E5; font-weight:650; }
.chip { display:inline-block; font-size:.7rem; font-weight:600; background:#F1F2F8; color:#4B5068;
  border-radius:999px; padding:.1rem .5rem; margin-left:.35rem; vertical-align:middle; }
.stop-body { font-size:.82rem; color:#555B72; margin-left:2.35rem; line-height:1.5; }
.stop-body a { color:#4F46E5; text-decoration:none; font-weight:600; }
.reason { font-style:italic; color:#6B7085; }
.note { background:#FFFBEB; border-radius:8px; padding:.2rem .45rem; color:#78350F; display:inline-block; margin-top:.15rem; }
.src { font-size:.72rem; color:#9095A8; }
[class*="st-key-acts_"] button { padding:.1rem .55rem; min-height:1.9rem; font-size:.85rem; }
[class*="st-key-acts_"] { margin-left:2.35rem; margin-top:.6rem; }  /* clear of the text above */

/* ---------- legend ---------- */
.legend { display:flex; flex-wrap:wrap; align-items:center; gap:.9rem; font-size:.78rem; color:#555B72;
  padding:0 .5rem; }
/* Streamlit gives markdown wrappers a -1rem bottom margin; undo it for the legend so it
   stays inside the card, with equal space above (map) and below (card edge). */
div:has(> .legend) { margin-bottom:0 !important; }
.st-key-map_card, .st-key-preview_card { padding-bottom:1rem; }
.legend i { display:inline-block; vertical-align:middle; margin-right:.35rem; }
.lg-dot { width:11px; height:11px; border-radius:50%; }
.lg-line { width:22px; height:0; border-top:3px solid #4F46E5; }
.lg-dash { width:22px; height:0; border-top:3px dashed #F59E0B; }

/* ---------- chat ---------- */
[data-testid="stChatMessage"] { background:transparent; padding:.35rem .2rem; }
.qa-hint { font-size:.8rem; color:#7A7F94; margin:.2rem 0 .4rem; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def step_title(n: int, text: str) -> None:
    st.markdown(f'<div class="step"><span class="step-n">{n}</span>{esc(text)}</div>', unsafe_allow_html=True)
