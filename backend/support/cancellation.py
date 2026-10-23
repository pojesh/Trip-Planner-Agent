"""Deterministic cancellation quotes under WH-POL-003.

The support agent explains these numbers; it never works them out itself.
"""

import secrets
from datetime import date, datetime, timedelta
from typing import Optional

from pydantic import BaseModel

from backend.domain.models import Booking, BookingStatus, FareType, Trip, TripType
from backend.errors import AppError
from backend.support import policy_rules as rules

QUOTE_TTL_MINUTES = 15


class CancellationQuote(BaseModel):
    quote_id: str
    trip_id: str
    booking_ref: str
    trip_title: str
    destination: str
    trip_type: TripType
    fare_type: FareType
    policy_version: str
    received_on: date  # the date the request counts as received (after the 18:00 IST cutoff)
    departure_date: date
    days_before_departure: int
    tier: str
    charge_percent: int
    refund_percent: int
    non_refundable: list[str]
    refund_working_days: int
    refund_expected_by: date
    notes: list[str]
    policy_doc: str
    created_at: str
    expires_at: str

    def for_agent(self) -> dict:
        """What the LLM sees: percentages and dates written out the way it should repeat them."""
        return {
            "quote_id": self.quote_id, "booking_ref": self.booking_ref, "trip": self.trip_title,
            "trip_type": self.trip_type.value, "fare_type": self.fare_type.value,
            "policy": f"{self.policy_doc} {self.policy_version}",
            "request_counted_on": self.received_on.isoformat(),
            "departure_date": self.departure_date.isoformat(),
            "days_before_departure": self.days_before_departure, "tier": self.tier,
            "cancellation_charge": f"{self.charge_percent}%", "refund": f"{self.refund_percent}%",
            "refund_applies_to": "the amount paid, after the non-refundable components are deducted",
            "non_refundable_components": self.non_refundable,
            "refund_timeline": f"{self.refund_working_days} working days, to the original payment mode",
            "refund_expected_by": self.refund_expected_by.isoformat(),
            "notes": self.notes, "quote_expires_at": self.expires_at,
        }


def received_on(now: datetime) -> date:
    """The IST calendar date a request made at `now` counts as received on."""
    local = now.astimezone(rules.IST)
    return local.date() + timedelta(days=1) if local.hour >= rules.CUTOFF_HOUR_IST else local.date()


def add_working_days(start: date, n: int) -> date:
    """`start` plus n working days (Monday-Friday)."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def check_cancellable(booking: Optional[Booking], trip: Trip, now: datetime) -> int:
    """Raise a clear AppError if the booking can't be cancelled; otherwise return days before departure."""
    if booking is None:
        raise AppError("not_booked", f"'{trip.title}' is not booked, so there is nothing to cancel.", http_status=409)
    if booking.status == BookingStatus.cancelled:
        raise AppError("already_cancelled",
                       f"Booking {booking.booking_ref} is already cancelled "
                       f"(cancellation ref {booking.cancellation_ref}).", http_status=409)
    days = (trip.start_date - received_on(now)).days
    if days < 0:
        raise AppError("already_departed",
                       f"Booking {booking.booking_ref} departed on {trip.start_date.isoformat()}. Bookings cannot be "
                       "cancelled after the departure date, and unused services are not refunded (no-show).",
                       http_status=409)
    return days


def make_quote(booking: Optional[Booking], trip: Trip, now: datetime) -> CancellationQuote:
    days = check_cancellable(booking, trip, now)
    charge, tier = rules.charge_for(booking.trip_type, booking.fare_type, days)
    on = received_on(now)
    notes = ["The cancellation charge is a percentage of the total package cost."]
    if days == 0:
        notes.append("Cancelling on the departure date carries a 100% charge.")
    if booking.trip_type == TripType.international:
        notes.append(rules.TCS_NOTE)
    notes.append(rules.SPECIAL_CASES_NOTE + " " + rules.CUSTOMER_CARE)
    working_days = rules.REFUND_WORKING_DAYS[booking.trip_type]
    return CancellationQuote(
        quote_id="Q-" + secrets.token_hex(6), trip_id=trip.id, booking_ref=booking.booking_ref,
        trip_title=trip.title, destination=trip.destination.label,
        trip_type=booking.trip_type, fare_type=booking.fare_type, policy_version=booking.policy_version,
        received_on=on, departure_date=trip.start_date, days_before_departure=days, tier=tier,
        charge_percent=charge, refund_percent=100 - charge,
        non_refundable=rules.NON_REFUNDABLE[booking.trip_type],
        refund_working_days=working_days, refund_expected_by=add_working_days(on, working_days),
        notes=notes, policy_doc=rules.CANCELLATION_DOC,
        created_at=now.isoformat(timespec="seconds"),
        expires_at=(now + timedelta(minutes=QUOTE_TTL_MINUTES)).isoformat(timespec="seconds"),
    )
