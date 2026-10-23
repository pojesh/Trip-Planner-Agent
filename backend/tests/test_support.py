"""Support assistant: policy rules, cancellation quotes, booking, the agent loop and its guards.

No network: a scripted LLM and a fake retriever stand in for Gemini and Chroma.
"""

import re
from datetime import date, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage

from backend.domain.models import (
    BookingStatus, FareType, Location, Trip, TripConstraints, TripStatus, TripType, utc_now,
)
from backend.errors import AppError
from backend.services.plan_service import PlanService
from backend.support import policy_rules as rules
from backend.support.agent import FALLBACK, SupportAgent, SupportContext, check_answer
from backend.support.cancellation import make_quote, received_on
from backend.support.retriever import Chunk
from backend.support.service import SupportService, trip_type_for
from backend.support.tracing import Tracer
from backend.tests.conftest import FakeNominatim, FakeOverpass, FakeRouter
from backend.tests.test_agent import ScriptedLLM, call


def ist(y, m, d, hh=10, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=rules.IST)


NOW = ist(2026, 5, 1)


def make_trip(repo, start: date, label: str = "Lisbon, Portugal", tid: str = "trip1", days: int = 2) -> Trip:
    trip = Trip(id=tid, title=f"Trip to {label.split(',')[0]}", status=TripStatus.generated,
                destination=Location(label=label, lat=38.72, lon=-9.14),
                origin=Location(label="Hotel", lat=38.716, lon=-9.141), origin_mode="search",
                start_date=start, end_date=start + timedelta(days=days - 1), daily_start_time="10:00",
                daily_end_time="20:00", transport_mode="walking", pace="moderate", interests=["culture"],
                constraints=TripConstraints(), created_at=utc_now(), updated_at=utc_now())
    repo.save_trip(trip)
    return trip


class FakeRetriever:
    """Returns fixed policy passages; scores below the threshold count as irrelevant."""
    threshold = 0.5

    def __init__(self, chunks=None):
        self.chunks = chunks if chunks is not None else [
            Chunk(cite="[WH-POL-003 p.2]", doc_code="WH-POL-003", title="Cancellation and Refund Policy",
                  version="v1", page=2, score=0.82, relevant=True, source="WH-POL-003.pdf",
                  text="International Standard: 30-44 days before departure, the cancellation charge is 50%."),
            Chunk(cite="[WH-POL-012 p.4]", doc_code="WH-POL-012", title="FAQ", version="v1", page=4,
                  score=0.31, relevant=False, source="WH-POL-012.pdf", text="Unrelated passage."),
        ]
        self.queries = []

    def search(self, query, trip_type=None, doc_code=None, k=None):
        self.queries.append(query)
        return self.chunks, {"embed_ms": 1.0, "query_ms": 2.0, "filter": None}

    def stats(self):
        return {"chunks": len(self.chunks)}


def svc_for(repo, llm=None, now=NOW, retriever=None):
    agent = SupportAgent(llm, "fake-model") if llm else None
    return SupportService(repo, retriever or FakeRetriever(), agent, clock=lambda: now)


def drain(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return stop.value, events


# ------------------------------------------------------------------ policy rules

@pytest.mark.parametrize("trip_type,fare,days,charge", [
    (TripType.domestic, FareType.standard, 60, 10), (TripType.domestic, FareType.standard, 45, 10),
    (TripType.domestic, FareType.standard, 44, 25), (TripType.domestic, FareType.standard, 30, 25),
    (TripType.domestic, FareType.standard, 29, 50), (TripType.domestic, FareType.standard, 7, 75),
    (TripType.domestic, FareType.standard, 6, 100), (TripType.domestic, FareType.standard, 0, 100),
    (TripType.international, FareType.standard, 60, 15), (TripType.international, FareType.standard, 59, 30),
    (TripType.international, FareType.standard, 45, 30), (TripType.international, FareType.standard, 30, 50),
    (TripType.international, FareType.standard, 15, 75), (TripType.international, FareType.standard, 14, 100),
    (TripType.domestic, FareType.flexi, 30, 0), (TripType.domestic, FareType.flexi, 29, 50),
    (TripType.international, FareType.flexi, 29, 75), (TripType.international, FareType.promo, 90, 100),
])
def test_tier_boundaries(trip_type, fare, days, charge):
    assert rules.charge_for(trip_type, fare, days)[0] == charge


def test_18_00_ist_cutoff_counts_as_next_day():
    assert received_on(ist(2026, 5, 1, 17, 59)) == date(2026, 5, 1)
    assert received_on(ist(2026, 5, 1, 18, 0)) == date(2026, 5, 2)


def test_trip_type_from_destination_country():
    assert trip_type_for("Chennai, Tamil Nadu, India", "India") == TripType.domestic
    assert trip_type_for("Lisbon, Portugal", "India") == TripType.international


# ------------------------------------------------------------------ booking + quotes

def test_book_saves_trip_and_blocks_double_booking(repo):
    make_trip(repo, date(2026, 6, 1))
    svc = svc_for(repo)
    trip = svc.book("trip1", FareType.flexi)
    b = trip.booking
    assert trip.status == TripStatus.saved  # a booked trip is never purged as a draft
    assert b.status == BookingStatus.booked and b.trip_type == TripType.international
    assert b.fare_type == FareType.flexi and b.policy_version == rules.POLICY_VERSION
    assert re.fullmatch(r"WH-\d{4}-\d{6}", b.booking_ref)
    with pytest.raises(AppError) as e:
        svc.book("trip1", FareType.standard)
    assert e.value.code == "already_booked"


def test_cannot_book_a_past_trip(repo):
    make_trip(repo, date(2026, 4, 1))
    with pytest.raises(AppError) as e:
        svc_for(repo).book("trip1", FareType.standard)
    assert e.value.code == "trip_in_past"


def test_booked_trip_cannot_be_deleted(repo):
    make_trip(repo, date(2026, 6, 1))
    svc_for(repo).book("trip1", FareType.standard)
    plans = PlanService(repo, FakeNominatim(), FakeOverpass(), FakeRouter(), None)
    with pytest.raises(AppError) as e:
        plans.delete_trip("trip1")
    assert e.value.code == "trip_booked"


def test_quote_for_international_standard(repo):
    make_trip(repo, date(2026, 5, 31))  # 30 days after 1 May
    trip = svc_for(repo).book("trip1", FareType.standard)
    q = make_quote(trip.booking, trip, NOW)
    assert q.days_before_departure == 30 and q.tier == "30-44 days"
    assert (q.charge_percent, q.refund_percent) == (50, 50)
    assert "Visa fees and visa service charges" in q.non_refundable
    assert any("TCS" in n for n in q.notes)
    assert q.refund_expected_by == date(2026, 6, 12)  # 30 working days after Fri 1 May


def test_quote_refuses_after_departure(repo):
    make_trip(repo, date(2026, 5, 3))
    trip = svc_for(repo, now=ist(2026, 5, 2)).book("trip1", FareType.standard)
    with pytest.raises(AppError) as e:
        make_quote(trip.booking, trip, ist(2026, 5, 4))
    assert e.value.code == "already_departed"


def _pending_quote(repo, svc, now=NOW):
    trip = repo.get_trip("trip1")
    q = make_quote(trip.booking, trip, now)
    repo.save_pending(q.quote_id, "cancel", trip.id, q.model_dump(mode="json"), q.created_at, q.expires_at)
    return q


def test_confirm_cancels_once_and_is_idempotent(repo):
    make_trip(repo, date(2026, 7, 15), label="Jaipur, Rajasthan, India")  # 75 days: domestic 10%
    svc = svc_for(repo)
    svc.book("trip1", FareType.standard)
    q = _pending_quote(repo, svc)
    first = svc.confirm_cancellation(q.quote_id)
    b = repo.get_booking("trip1")
    assert b.status == BookingStatus.cancelled and b.charge_percent == 10 and b.refund_percent == 90
    assert b.refund_status == "initiated" and re.fullmatch(r"CXL-\d{4}-\d{6}", b.cancellation_ref)
    assert not first["already"] and b.cancellation_ref in first["message"]
    assert [e["kind"] for e in first["trace"]["events"]] == ["check", "db_write"]
    again = svc.confirm_cancellation(q.quote_id)
    assert again["already"] and repo.get_booking("trip1").cancellation_ref == b.cancellation_ref


def test_expired_quote_is_refused(repo):
    make_trip(repo, date(2026, 7, 15))
    svc = svc_for(repo)
    svc.book("trip1", FareType.standard)
    q = _pending_quote(repo, svc)
    late = svc_for(repo, now=NOW + timedelta(minutes=16))
    with pytest.raises(AppError) as e:
        late.confirm_cancellation(q.quote_id)
    assert e.value.code == "quote_expired"
    assert repo.get_booking("trip1").status == BookingStatus.booked


# ------------------------------------------------------------------ guards + agent

def test_check_answer_flags_unknown_citations_and_numbers():
    sources = "the cancellation charge is 50 per cent; departure 2026-05-31"
    ok, cites = check_answer("It's 50% [WH-POL-003 p.2], departing 2026-05-31.", {"[WH-POL-003 p.2]"}, sources)
    assert ok == [] and cites == ["[WH-POL-003 p.2]"]
    narrow_space, _ = check_answer("The charge is 50 %.", set(), sources)  # gpt-oss writes this
    assert narrow_space == []
    odd_hyphens, _ = check_answer("Expected by 2026‑12‑04.", set(), sources)  # not in sources
    assert odd_hyphens == ["'2026-12-04' does not appear in any tool result or system fact."]
    bad, _ = check_answer("It's 5% [WH-POL-009 p.1] or 100%.", {"[WH-POL-003 p.2]"}, sources)
    assert any("WH-POL-009" in p for p in bad) and any("'5%'" in p for p in bad) and any("'100%'" in p for p in bad)


def _ctx(repo, retriever=None):
    return SupportContext(repo=repo, retriever=retriever or FakeRetriever(), tracer=Tracer("fake"), now=NOW)


def test_agent_answers_with_checked_citation(repo):
    llm = ScriptedLLM([
        call("search_policies", {"query": "international cancellation 30 days"}),
        AIMessage(content="Cancelling 30-44 days before departure costs 50% [WH-POL-003 p.2].",
                  usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}),
    ])
    ctx = _ctx(repo)
    result, events = drain(SupportAgent(llm).run(ctx, [], "What if I cancel a month before?"))
    assert result.kind == "reply" and [c["cite"] for c in result.citations] == ["[WH-POL-003 p.2]"]
    kinds = [e["kind"] for e in events]
    assert kinds == ["llm", "retrieval", "tool", "llm", "guard"] and events[-1]["passed"]
    summary = ctx.tracer.summary()
    assert summary["llm_calls"] == 2 and summary["tokens"] == {"input": 100, "output": 20, "total": 120}
    assert summary["tokens_partial"] and summary["docs_retrieved"] == ["WH-POL-003", "WH-POL-012"]


def test_fullwidth_bracket_citations_are_normalised_and_checked(repo):
    llm = ScriptedLLM([
        call("search_policies", {"query": "international cancellation"}),
        AIMessage(content="It's 50% 【WH-POL-003 p.2】."),
    ])
    result, events = drain(SupportAgent(llm).run(_ctx(repo), [], "refund?"))
    assert result.message == "It's 50% [WH-POL-003 p.2]."
    assert [c["cite"] for c in result.citations] == ["[WH-POL-003 p.2]"]
    assert events[-1]["citations"] == ["[WH-POL-003 p.2]"]


def test_agent_retries_once_then_falls_back(repo):
    llm = ScriptedLLM([
        AIMessage(content="You always get a 75% refund."),       # no source at all
        AIMessage(content="Fine: refunds are 80% [WH-POL-001 p.1]."),  # still invented
    ])
    result, events = drain(SupportAgent(llm).run(_ctx(repo), [], "refund?"))
    assert result.message == FALLBACK and result.guarded
    assert "[App check]" in llm.seen[1][-1].content
    assert [e["passed"] for e in events if e["kind"] == "guard"] == [False, False]


def test_irrelevant_search_returns_no_relevant_policy(repo):
    low = [Chunk(cite="[WH-POL-012 p.1]", doc_code="WH-POL-012", title="FAQ", version="v1", page=1,
                 score=0.2, relevant=False, text="x", source="x.pdf")]
    llm = ScriptedLLM([call("search_policies", {"query": "weather in Lisbon"}),
                       AIMessage(content="I don't have that information.")])
    result, _ = drain(SupportAgent(llm).run(_ctx(repo, FakeRetriever(low)), [], "What's the weather?"))
    assert "NO_RELEVANT_POLICY" in llm.seen[1][-1].content and result.kind == "reply"


def test_agent_cancellation_needs_confirmation(repo, monkeypatch):
    import backend.support.cancellation as cancellation
    monkeypatch.setattr(cancellation.secrets, "token_hex", lambda n: "abc123")
    make_trip(repo, date(2026, 5, 31))
    ref = svc_for(repo).book("trip1", FareType.standard).booking.booking_ref
    llm = ScriptedLLM([
        call("list_bookings", {}),
        call("quote_cancellation", {"booking_ref": ref}, cid="c2"),
        call("request_cancellation_confirmation", {"quote_id": "Q-nope", "message": "x"}, cid="c3"),
        call("request_cancellation_confirmation",
             {"quote_id": "Q-abc123", "message": f"Cancelling {ref} costs 50%, refund 50%."}, cid="c4"),
    ])
    result, events = drain(SupportAgent(llm).run(_ctx(repo), [], "Cancel my Lisbon trip"))
    assert "Unknown quote_id" in llm.seen[3][-1].content  # a made-up quote id is rejected
    assert result.kind == "confirm" and result.quote.quote_id == "Q-abc123" and "50%" in result.message
    assert repo.get_pending("Q-abc123")["used_at"] is None
    assert repo.get_booking("trip1").status == BookingStatus.booked  # nothing cancelled until confirmed


def test_confirm_message_with_invented_numbers_uses_quote_wording(repo, monkeypatch):
    import backend.support.cancellation as cancellation
    monkeypatch.setattr(cancellation.secrets, "token_hex", lambda n: "abc123")
    make_trip(repo, date(2026, 5, 31))
    ref = svc_for(repo).book("trip1", FareType.standard).booking.booking_ref
    llm = ScriptedLLM([
        call("quote_cancellation", {"booking_ref": ref}),
        call("request_cancellation_confirmation", {"quote_id": "Q-abc123", "message": "You get 95% back!"}),
    ])
    result, _ = drain(SupportAgent(llm).run(_ctx(repo), [], "cancel it"))
    assert "95%" not in result.message and "50% cancellation charge" in result.message


def test_search_only_mode_without_gemini(repo):
    svc = svc_for(repo)
    result, events = drain(svc.chat("cancellation charges"))
    assert result is None and events[-1]["type"] == "result"
    final = events[-1]
    assert "AI answers are turned off" in final["message"] and final["citations"][0]["cite"] == "[WH-POL-003 p.2]"
    assert final["trace"]["summary"]["llm_calls"] == 0 and final["trace"]["summary"]["tokens"] is None
    assert [m["role"] for m in repo.list_support_messages()] == ["user", "assistant"]
    assert repo.list_traces()[0]["id"] == final["trace"]["id"]


def test_tracer_reports_no_tokens_when_provider_sends_none():
    t = Tracer("m")
    t.add("llm", "call 1", t.clock(), tokens=None)
    assert t.summary()["tokens"] is None and t.summary()["llm_calls"] == 1


# ------------------------------------------------------------------ LLM provider config

def test_llm_provider_switch_picks_the_right_key_and_model():
    from backend.config import Settings
    from backend.services.agent import build_llm, provider_name

    s = Settings(_env_file=None, llm_provider="Groq", groq_api_key="gsk-test", gemini_api_key="")
    assert s.llm_provider == "groq" and s.agent_enabled and s.llm_model == "openai/gpt-oss-120b"
    assert s.llm_key_name == "GROQ_API_KEY"
    assert not Settings(_env_file=None, llm_provider="gemini", groq_api_key="gsk-test").agent_enabled
    with pytest.raises(ValueError):
        Settings(_env_file=None, llm_provider="openai")
    pytest.importorskip("langchain_groq")
    llm = build_llm("groq", "gsk-test", s.llm_model, 10)
    assert provider_name(llm) == "Groq" and hasattr(llm, "bind_tools")


# ------------------------------------------------------------------ ingestion (PyMuPDF + Chroma)

def test_ingest_pdf_and_search(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    pytest.importorskip("langchain_chroma")
    from langchain_core.embeddings import DeterministicFakeEmbedding

    from backend.support.ingest import build_index, pdf_chunks
    from backend.support.retriever import PolicyRetriever

    data = tmp_path / "data"
    data.mkdir()
    pdf = pymupdf.open()
    for n, body in enumerate(["1. Purpose\nThis policy explains cancellations.",
                              "4. International Standard\nThe cancellation charge is 50%."], start=1):
        page = pdf.new_page()
        page.insert_text((40, 50), "Doc: WH-POL-003 | Title: Cancellation and Refund Policy | Version: v1 | "
                                   "Effective: 2026-04-01 | Applies to: domestic and international", fontsize=7)
        page.insert_text((40, 90), body, fontsize=11)
        page.insert_text((40, 800), "Wayfinder Holidays Pvt. Ltd. is a fictional company.", fontsize=7)
    pdf.save(data / "WH-POL-003_Cancellation.pdf")

    docs = pdf_chunks(data / "WH-POL-003_Cancellation.pdf")
    assert [d.metadata["page"] for d in docs] == [1, 2]
    m = docs[1].metadata
    assert m["doc_code"] == "WH-POL-003" and m["version"] == "v1" and m["international"] and m["domestic"]
    assert "Doc:" not in docs[1].page_content and "fictional" not in docs[1].page_content

    emb = DeterministicFakeEmbedding(size=32)
    _, manifest = build_index(data, tmp_path / "chroma", emb, "fake")
    assert manifest["chunks"] == 2
    retriever = PolicyRetriever(data, tmp_path / "chroma", "fake", threshold=-1.0, top_k=2, embeddings=emb)
    chunks, timing = retriever.search("cancellation charge", trip_type="international")
    assert len(chunks) == 2 and {c.cite for c in chunks} == {"[WH-POL-003 p.1]", "[WH-POL-003 p.2]"}
    assert "embed_ms" in timing and retriever.stats()["pdfs"] == 1
