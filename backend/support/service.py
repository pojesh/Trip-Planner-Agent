"""SupportService: booking, the support chat (agent + Dev Board traces) and confirmed cancellations."""

import logging
import secrets
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional

from backend.domain.models import Booking, BookingStatus, FareType, Trip, TripStatus, TripType, utc_now
from backend.errors import AppError, not_found
from backend.repositories.sqlite_repo import SqliteRepo
from backend.support import policy_rules as rules
from backend.support.agent import SupportAgent, SupportContext, SupportResult
from backend.support.cancellation import CancellationQuote, make_quote, received_on
from backend.support.retriever import PolicyRetriever
from backend.support.tracing import Tracer

log = logging.getLogger(__name__)

CHAT_HISTORY = 10


def new_ref(prefix: str) -> str:
    """'WH-2026-004211' / 'CXL-2026-918273'."""
    return f"{prefix}-{datetime.now(rules.IST).year}-{secrets.randbelow(10**6):06d}"


def trip_type_for(destination_label: str, home_country: str) -> TripType:
    """Nominatim labels end with the country ("Chennai, Tamil Nadu, India")."""
    country = destination_label.split(",")[-1].strip().lower()
    return TripType.domestic if country == home_country.strip().lower() else TripType.international


class SupportService:
    def __init__(self, repo: SqliteRepo, retriever: PolicyRetriever, agent: Optional[SupportAgent],
                 home_country: str = "India", model_name: Optional[str] = None, key_name: str = "the LLM API key",
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.repo = repo
        self.retriever = retriever
        self.agent = agent  # None when no LLM API key is set: search-only answers
        self.home_country = home_country
        self.model_name = model_name
        self.key_name = key_name
        self.clock = clock

    # ================================================================ booking

    def book(self, trip_id: str, fare_type: FareType) -> Trip:
        trip = self.repo.get_trip(trip_id)
        if trip is None:
            raise not_found("Trip")
        if trip.booking and trip.booking.status == BookingStatus.booked:
            raise AppError("already_booked", f"This trip is already booked ({trip.booking.booking_ref}).",
                           http_status=409)
        if trip.start_date < self.clock().astimezone(rules.IST).date():
            raise AppError("trip_in_past", "This trip starts in the past, so it can't be booked. "
                           "Change the dates by planning a new trip.", http_status=409)
        if trip.status != TripStatus.saved:  # a booked trip must never be purged as an old draft
            trip.status = TripStatus.saved
            trip.updated_at = utc_now()
            self.repo.save_trip(trip)
        self.repo.save_booking(Booking(
            trip_id=trip.id, booking_ref=new_ref("WH"), status=BookingStatus.booked,
            trip_type=trip_type_for(trip.destination.label, self.home_country), fare_type=fare_type,
            policy_version=rules.POLICY_VERSION, booked_at=utc_now()))
        return self.repo.get_trip(trip_id)

    def list_bookings(self) -> list[Booking]:
        return self.repo.list_bookings()

    # ================================================================ chat

    def chat(self, message: str, trip_id: Optional[str] = None) -> Iterator[dict]:
        tracer = Tracer(self.model_name if self.agent else None)
        history = self.repo.list_support_messages(CHAT_HISTORY)
        self.repo.add_support_message("user", message, {"trip_id": trip_id})
        text = message
        if trip_id and (trip := self.repo.get_trip(trip_id)):
            text += f"\n\n(The customer is looking at trip {trip.id}: '{trip.title}'.)"
        ctx = SupportContext(repo=self.repo, retriever=self.retriever, tracer=tracer, now=self.clock(),
                             home_country=self.home_country)
        yield {"type": "stage", "label": "Reading your question…"}
        try:
            if self.agent:
                result = yield from self.agent.run(ctx, history, text)
            else:
                result = yield from self._search_only(ctx, message)
        except AppError as e:
            self._save_trace(tracer, message, error=e.message)
            raise
        except Exception:
            log.exception("support chat failed")
            self._save_trace(tracer, message, error="internal error")
            raise

        quote = result.quote.model_dump(mode="json") if result.quote else None
        cites = [c["cite"] for c in result.citations]
        self.repo.add_support_message("assistant", result.message,
                                      {"citations": cites, "sources": result.citations, "quote": quote,
                                       "guarded": result.guarded})
        trace = self._save_trace(tracer, message)
        yield {"type": "result", "message": result.message, "kind": result.kind, "citations": result.citations,
               "quote": quote, "guarded": result.guarded, "trace": trace}

    def _search_only(self, ctx: SupportContext, message: str) -> Iterator[dict]:
        """No LLM key: show the best-matching policy passages instead of an AI answer."""
        off = f"AI answers are turned off (no {self.key_name})"
        t = ctx.tracer.clock()
        chunks, timing = self.retriever.search(message)
        yield ctx.tracer.add("retrieval", f"Policy search: {message[:60]}", t, query=message,
                             threshold=self.retriever.threshold, **timing, chunks=[c.to_dict() for c in chunks])
        good = [c for c in chunks if c.relevant][:3]
        if not good:
            return SupportResult("reply", f"{off}, and no policy section matched your question. "
                                          + rules.CUSTOMER_CARE + ".")
        parts = [f"**{c.title}** {c.cite}\n\n> {c.text[:500].strip()}…" for c in good]
        ctx.tracer.cited = [c.cite for c in good]
        return SupportResult("reply", f"{off}, so here are the most relevant policy sections:\n\n"
                                      + "\n\n".join(parts),
                             citations=[c.to_dict() for c in good])

    def _save_trace(self, tracer: Tracer, user_message: str, error: Optional[str] = None) -> dict:
        summary = {**tracer.summary(), "error": error}
        trace = {"id": "T-" + secrets.token_hex(6), "at": datetime.now(timezone.utc).isoformat(),
                 "user_message": user_message, "summary": summary, "events": tracer.events}
        try:
            self.repo.save_trace(trace["id"], trace["at"], user_message, summary, tracer.events)
        except Exception:  # the Dev Board must never break the chat
            log.exception("could not save the trace")
        return trace

    # ================================================================ confirmed cancellation

    def confirm_cancellation(self, quote_id: str) -> dict:
        tracer = Tracer()
        pending = self.repo.get_pending(quote_id)
        if pending is None or pending["kind"] != "cancel":
            raise not_found("Cancellation quote")
        quote = CancellationQuote(**pending["payload"])
        trip = self.repo.get_trip(quote.trip_id)
        if trip is None:
            raise not_found("Trip")
        booking = trip.booking
        if pending["used_at"] and booking and booking.status == BookingStatus.cancelled:  # double click
            return {"booking": booking.model_dump(mode="json"), "message": self._done_message(booking),
                    "already": True}

        now = self.clock()
        expired = now > datetime.fromisoformat(quote.expires_at) or received_on(now) != quote.received_on
        t = tracer.clock()
        fresh = make_quote(booking, trip, now)  # raises if already cancelled or departed
        tracer.add("check", "Re-checked the quote", t, expired=expired,
                   charge_then=f"{quote.charge_percent}%", charge_now=f"{fresh.charge_percent}%")
        if expired or fresh.charge_percent != quote.charge_percent:
            self._save_trace(tracer, f"[Confirm cancellation {quote.booking_ref}]", error="quote expired")
            raise AppError("quote_expired", "This cancellation quote has expired, so nothing was cancelled. "
                           "Ask the assistant to quote again; the charge may have changed.",
                           retryable=True, http_status=409)

        t = tracer.clock()
        cancelled = booking.model_copy(update={
            "status": BookingStatus.cancelled, "cancelled_at": utc_now(), "cancellation_ref": new_ref("CXL"),
            "cancellation_reason": "Cancelled by the customer through the support assistant",
            "charge_percent": quote.charge_percent, "refund_percent": quote.refund_percent,
            "refund_status": "initiated", "refund_expected_by": quote.refund_expected_by})
        if not self.repo.cancel_booking(cancelled, quote_id):
            raise AppError("already_cancelled", f"Booking {booking.booking_ref} is no longer active.", http_status=409)
        tracer.add("db_write", "Booking cancelled", t, table="bookings", booking_ref=booking.booking_ref,
                   change="booked → cancelled", cancellation_ref=cancelled.cancellation_ref,
                   refund=f"{quote.refund_percent}%", refund_expected_by=quote.refund_expected_by.isoformat())
        message = self._done_message(cancelled)
        self.repo.add_support_message("assistant", message, {"cancelled": cancelled.booking_ref})
        trace = self._save_trace(tracer, f"[Confirm cancellation {quote.booking_ref}]")
        return {"booking": cancelled.model_dump(mode="json"), "message": message, "already": False, "trace": trace}

    def keep_booking(self, quote_id: str) -> dict:
        pending = self.repo.get_pending(quote_id)
        if pending is None:
            raise not_found("Cancellation quote")
        ref = pending["payload"]["booking_ref"]
        message = f"No problem, booking {ref} stays as it is. Nothing was changed."
        self.repo.add_support_message("assistant", message)
        return {"message": message}

    @staticmethod
    def _done_message(b: Booking) -> str:
        return (f"Booking {b.booking_ref} is cancelled. Your cancellation reference is {b.cancellation_ref}. "
                f"The cancellation charge is {b.charge_percent}%, and a {b.refund_percent}% refund of the amount "
                f"paid (after non-refundable components) has been initiated to your original payment mode. "
                f"It is expected by {b.refund_expected_by.isoformat()}; banks may take a further 5-7 working days.")

    # ================================================================ history, traces, index

    def history(self) -> list[dict]:
        return self.repo.list_support_messages()

    def clear_history(self) -> None:
        self.repo.clear_support_messages()

    def traces(self, limit: int = 20) -> list[dict]:
        return self.repo.list_traces(limit)

    def index_stats(self) -> dict:
        return self.retriever.stats()

    def reindex(self) -> dict:
        return self.retriever.reindex()
