"""Help & bookings: chat with the support assistant (RAG over policy PDFs) plus a Dev Board.

The Dev Board shows each step of a turn as the backend traced it: LLM calls (latency, tokens),
policy searches (chunks, scores, pages), tool calls, grounding checks and database writes.
"""

from datetime import datetime

import streamlit as st

import api_client as api
from api_client import ApiError

SUGGESTIONS = {
    ":material/event_busy: Cancellation policy": "What is the cancellation policy for my trips?",
    ":material/luggage: My bookings": "Which of my trips are booked?",
    ":material/public: International travel": "What documents do I need for an international trip?",
    ":material/currency_rupee: Refunds": "How long does a refund take?",
}

KIND = {  # trace kind -> (icon, label)
    "llm": (":material/psychology:", "LLM"),
    "retrieval": (":material/manage_search:", "Retrieval"),
    "tool": (":material/build:", "Tool"),
    "guard": (":material/verified_user:", "Guard"),
    "check": (":material/fact_check:", "Check"),
    "db_write": (":material/database:", "DB write"),
    "error": (":material/error:", "Error"),
}


def _md(text: str) -> str:
    return text.replace("$", "\\$")  # avoid accidental LaTeX rendering


def _ms(value) -> str:
    if value is None:
        return "—"
    return f"{value:.0f} ms" if value < 1000 else f"{value / 1000:.2f} s"


def _tokens(t) -> str:
    return f"{t['total']:,}" if t else "n/a"


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %H:%M:%S")
    except (TypeError, ValueError):
        return iso or ""


def _event_line(ev: dict) -> str:
    icon, _ = KIND.get(ev["kind"], (":material/circle:", ev["kind"]))
    extra = ""
    if ev["kind"] == "llm":
        extra = f" · {_tokens(ev.get('tokens'))} tokens"
        if ev.get("tool_calls"):
            extra += " · calls " + ", ".join(ev["tool_calls"])
    elif ev["kind"] == "retrieval":
        hits = sum(1 for c in ev.get("chunks", []) if c["relevant"])
        extra = f" · {hits}/{len(ev.get('chunks', []))} relevant"
    elif ev["kind"] == "guard":
        extra = " · passed" if ev.get("passed") else " · failed: " + "; ".join(ev.get("problems", []))[:120]
    return f"{icon} {ev['label']} · {_ms(ev.get('ms'))}{extra}"


# ------------------------------------------------------------------ callbacks

def _confirm(quote_id: str):
    ss = st.session_state
    try:
        res = api.support_confirm(quote_id)
        ss.flash = "Booking cancelled" if not res.get("already") else "Already cancelled"
        if res.get("trace"):
            ss.trace_pick = res["trace"]["id"]
        if ss.get("trip") and ss.trip["id"] == res["booking"]["trip_id"]:
            ss.trip = api.get_trip(ss.trip["id"])
    except ApiError as e:
        ss.flash_error = e.message
    ss.resolved_quotes.add(quote_id)


def _keep(quote_id: str):
    ss = st.session_state
    try:
        api.support_keep(quote_id)
    except ApiError as e:
        ss.flash_error = e.message
    ss.resolved_quotes.add(quote_id)


def _pick_suggestion():
    ss = st.session_state
    ss.support_picked = SUGGESTIONS.get(ss.support_chip)
    ss.support_chip = None  # send it once, even if the request fails


def _clear():
    try:
        api.support_clear()
    except ApiError as e:
        st.session_state.flash_error = e.message


def _reindex():
    ss = st.session_state
    try:
        stats = api.support_reindex()
        ss.flash = f"Index rebuilt: {stats['chunks']} chunks from {stats['pdfs']} PDFs"
    except ApiError as e:
        ss.flash_error = e.message


# ------------------------------------------------------------------ chat

def _sources(sources: list[dict]) -> None:
    with st.expander(f"Sources ({len(sources)})", icon=":material/description:", type="compact"):
        for s in sources:
            st.markdown(f"**{s['cite']}** {s['title']} · {s['version']} · page {s['page']} · "
                        f"relevance {s['score']:.2f}")
            st.caption(_md(s["text"][:500]))


def _confirm_card(quote: dict) -> None:
    with st.container(border=True):
        st.markdown(":red[:material/event_busy:] **Confirm cancellation**")
        rows = {
            "Booking": f"{quote['booking_ref']} · {quote['trip_title']}",
            "Trip type / fare": f"{quote['trip_type'].capitalize()} · {quote['fare_type'].capitalize()}",
            "Departure": f"{quote['departure_date']} ({quote['days_before_departure']} days away)",
            "Tier": quote["tier"],
            "Cancellation charge": f"{quote['charge_percent']}% of the package cost",
            "Refund": f"{quote['refund_percent']}% of the amount paid, after non-refundable items",
            "Non-refundable": "; ".join(quote["non_refundable"]),
            "Refund expected by": f"{quote['refund_expected_by']} ({quote['refund_working_days']} working days)",
            "Quote valid until": _when(quote["expires_at"]),
            "Policy": f"{quote['policy_doc']} {quote['policy_version']}",
        }
        st.table([{"Item": k, "Detail": v} for k, v in rows.items()], hide_index=True)
        with st.container(horizontal=True):
            st.button("Confirm cancellation", icon=":material/check:", type="primary",
                      on_click=_confirm, args=(quote["quote_id"],))
            st.button("Keep my booking", on_click=_keep, args=(quote["quote_id"],))


def _message(m: dict) -> None:
    meta = m.get("meta") or {}
    if m["role"] == "user":
        with st.chat_message("user"):
            st.markdown(_md(m["content"]))
        return
    with st.chat_message("assistant", avatar=":material/support_agent:"):
        st.markdown(_md(m["content"]))
        if meta.get("guarded"):
            st.badge("Answer withheld: it couldn't be verified against the sources", icon=":material/shield:",
                     color="orange")
        if meta.get("cancelled"):
            st.badge(f"{meta['cancelled']} cancelled", icon=":material/check_circle:", color="green")
        if meta.get("sources"):
            _sources(meta["sources"])


def _send(text: str, box) -> None:
    ss = st.session_state
    with box:
        with st.chat_message("user"):
            st.markdown(_md(text))
        with st.chat_message("assistant", avatar=":material/support_agent:"):
            with st.status(":shimmer[Working]", type="compact") as status:
                try:
                    for ev in api.support_chat(text):
                        if ev["type"] == "stage":
                            status.update(label=f":shimmer[{ev['label']}]")
                        elif ev["type"] == "trace":
                            st.markdown(_event_line(ev))
                        elif ev["type"] == "result":
                            secs = ev["trace"]["summary"]["total_ms"] / 1000
                            status.update(label=f"Worked for {secs:.1f} s", state="complete")
                            ss.trace_pick = ev["trace"]["id"]
                            st.rerun()
                        elif ev["type"] == "error":
                            status.update(label="Something went wrong", state="error")
                            st.error(ev["message"])
                            return
                except ApiError as e:
                    status.update(label="Something went wrong", state="error")
                    st.error(e.message)


def _chat(agent_on: bool) -> None:
    ss = st.session_state
    try:
        history = api.support_history()
    except ApiError as e:
        st.error(e.message)
        return

    with st.container(horizontal=True, horizontal_alignment="right"):
        st.button("New conversation", icon=":material/restart_alt:", on_click=_clear, type="tertiary")
    if not agent_on:
        st.info(f"No `{st.session_state.health.get('key_name', 'LLM API key')}` in `.env`: the assistant shows "
                "matching policy sections instead of AI answers.",
                icon=":material/info:")

    box = st.container(height=560, border=False)
    with box:
        if not history:
            st.caption("Ask about your bookings, cancellations, refunds, visas or any Wayfinder Holidays policy. "
                       "Answers cite the policy documents they come from.")
        for m in history:
            _message(m)
        last = history[-1] if history else None
        quote = (last.get("meta") or {}).get("quote") if last and last["role"] == "assistant" else None
        if quote and quote["quote_id"] not in ss.resolved_quotes:
            _confirm_card(quote)

    if not history:
        st.pills("Try asking", list(SUGGESTIONS), label_visibility="collapsed", key="support_chip",
                 on_change=_pick_suggestion)
    prompt = st.chat_input("Ask about bookings, cancellations or policies", key="support_in",
                           submit_mode="disable", max_chars=1000)
    text = prompt or ss.pop("support_picked", None)
    if text:
        _send(text, box)


# ------------------------------------------------------------------ dev board

def _event_details(ev: dict) -> None:
    kind = ev["kind"]
    if kind == "llm":
        t = ev.get("tokens")
        st.markdown(f"Model `{ev.get('model') or '—'}` · step {ev.get('step')} · latency {_ms(ev.get('ms'))}")
        st.markdown(f"Tokens: input **{t['input']:,}** · output **{t['output']:,}** · total **{t['total']:,}**"
                    if t else "Tokens: n/a (the provider reported no usage)")
        if ev.get("tool_calls"):
            st.markdown("Requested tools: " + ", ".join(f"`{n}`" for n in ev["tool_calls"]))
    elif kind == "retrieval":
        st.markdown(f"Query: *{ev.get('query')}*")
        st.caption(f"Embedding {_ms(ev.get('embed_ms'))} · vector search {_ms(ev.get('query_ms'))} · "
                   f"threshold {ev.get('threshold')} · filter {ev.get('filter') or 'none'}")
        chunks = ev.get("chunks", [])
        if chunks:
            st.dataframe(
                [{"cite": c["cite"], "document": c["title"], "version": c["version"], "page": c["page"],
                  "score": c["score"], "used": c["relevant"], "text": c["text"]} for c in chunks],
                hide_index=True,
                column_config={
                    "score": st.column_config.NumberColumn("Score", format="%.3f"),
                    "used": st.column_config.CheckboxColumn("Above threshold"),
                    "text": st.column_config.TextColumn("Chunk text", width="large"),
                })
        else:
            st.caption("No chunks returned (is the index empty?).")
    elif kind == "tool":
        st.markdown("Arguments")
        st.json(ev.get("args") or {}, expanded=False)
        st.markdown("Result")
        st.code(ev.get("result") or "", language=None, wrap_lines=True)
    elif kind == "guard":
        if ev.get("passed"):
            st.success("All citations and numbers were found in this turn's sources.", icon=":material/check:")
        else:
            for p in ev.get("problems", []):
                st.warning(p, icon=":material/warning:")
        if ev.get("citations"):
            st.markdown("Citations: " + ", ".join(ev["citations"]))
    else:
        st.json({k: v for k, v in ev.items() if k not in ("type", "kind", "label")}, expanded=True)


def _trace_view(trace: dict) -> None:
    s = trace["summary"]
    t = s.get("tokens")
    with st.container(horizontal=True):
        st.metric("Total latency", _ms(s["total_ms"]), border=True)
        st.metric("LLM calls", s["llm_calls"], help=f"Time in LLM calls: {_ms(s.get('llm_ms'))}", border=True)
        st.metric("Tokens", _tokens(t), border=True,
                  help=(f"Input {t['input']:,} · output {t['output']:,}" + (" (some calls reported none)"
                                                                            if s.get("tokens_partial") else ""))
                  if t else "The provider reported no token usage")
        st.metric("Tool calls", s["tool_calls"], border=True)
        st.metric("Docs retrieved", len(s.get("docs_retrieved", [])), border=True)
    st.caption(f"{_when(trace['at'])} · model {s.get('model') or 'none (search-only)'} · "
               f"cited: {', '.join(s.get('citations') or []) or 'none'}")
    if s.get("error"):
        st.error(f"This turn failed: {s['error']}")

    events = trace["events"]
    if not events:
        st.caption("No steps recorded.")
        return
    st.dataframe(
        [{"#": i + 1, "step": KIND.get(e["kind"], ("", e["kind"]))[1], "label": e["label"],
          "latency_ms": e.get("ms"), "start_ms": round((e.get("at_ms") or 0) - (e.get("ms") or 0), 1),
          "tokens": (e.get("tokens") or {}).get("total") if e["kind"] == "llm" else None}
         for i, e in enumerate(events)],
        hide_index=True,
        column_config={
            "latency_ms": st.column_config.NumberColumn("Latency (ms)", format="%.1f"),
            "start_ms": st.column_config.NumberColumn("Started at (ms)", format="%.1f"),
            "tokens": st.column_config.NumberColumn("Tokens"),
        })
    for i, ev in enumerate(events, start=1):
        icon, _ = KIND.get(ev["kind"], (":material/circle:", ev["kind"]))
        with st.expander(f"{i}. {ev['label']} · {_ms(ev.get('ms'))}", icon=icon):
            _event_details(ev)


def _index_view() -> None:
    try:
        with st.spinner("Loading the index (the first time loads the embedding model)…"):
            stats = api.support_index()
    except ApiError as e:
        st.error(e.message)
        return
    with st.container(horizontal=True):
        st.metric("PDFs", stats["pdfs"], border=True)
        st.metric("Chunks", stats["chunks"], border=True)
        st.metric("Relevance threshold", stats["threshold"], border=True)
    st.caption(f"Embedding model `{stats['model']}` · built {_when(stats.get('built_at')) or 'never'} · "
               f"source folder `{stats['data_dir']}`")
    if not stats["pdfs"]:
        st.warning("No PDFs indexed yet. Put the policy PDFs in `rag/data/`, then rebuild.",
                   icon=":material/folder_open:")
    for d in stats.get("docs", []):
        st.markdown(f"- {d}")
    st.button("Rebuild index", icon=":material/refresh:", on_click=_reindex,
              help="Re-read every PDF in rag/data and re-embed it")


def _dev_board() -> None:
    ss = st.session_state
    with st.container(border=True):
        st.markdown("**:material/monitoring: Dev board**")
        view = st.segmented_control("Dev board view", ["Turns", "Knowledge base"], default="Turns",
                                    key="dev_view", label_visibility="collapsed") or "Turns"
        if view == "Knowledge base":
            _index_view()
            return
        try:
            traces = api.support_traces(20)
        except ApiError as e:
            st.error(e.message)
            return
        if not traces:
            st.caption("Each assistant turn shows up here: steps, latency, tokens and the documents retrieved.")
            return
        by_id = {t["id"]: t for t in traces}
        if ss.get("trace_pick") not in by_id:
            ss.trace_pick = traces[0]["id"]
        st.selectbox("Turn", list(by_id), key="trace_pick",
                     format_func=lambda tid: f"{_when(by_id[tid]['at'])} · {by_id[tid]['user_message'][:60]}")
        _trace_view(by_id[ss.trace_pick])


def render_support(agent_on: bool) -> None:
    ss = st.session_state
    ss.setdefault("resolved_quotes", set())
    st.markdown("#### :material/support_agent: Help & bookings")
    st.caption("Wayfinder Holidays support: answers come only from our policy documents and your own trips "
               "and bookings.")
    left, right = st.columns([1.1, 1], gap="large")
    with left:
        _chat(agent_on)
    with right:
        _dev_board()
