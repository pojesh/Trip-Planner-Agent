"""Cancellation rules of WH-POL-003 (Cancellation and Refund Policy, v1).

The code applies these numbers; the policy PDFs in rag/data state the same rules in words.
Keep the two in step when either changes.
"""

from datetime import timedelta, timezone

from backend.domain.models import FareType, TripType

POLICY_VERSION = "v1"
POLICY_EFFECTIVE = "2026-04-01"
CANCELLATION_DOC = "WH-POL-003"

IST = timezone(timedelta(hours=5, minutes=30), "IST")
CUTOFF_HOUR_IST = 18  # requests at or after 18:00 IST count as received the next day

# (minimum days before departure, cancellation charge %), checked from the top.
STANDARD_TIERS = {
    TripType.domestic: [(45, 10), (30, 25), (15, 50), (7, 75), (0, 100)],
    TripType.international: [(60, 15), (45, 30), (30, 50), (15, 75), (0, 100)],
}
FLEXI_FREE_DAYS = 30  # Flexi: 0% charge this many days or more before departure
PROMO_CHARGE = 100

REFUND_WORKING_DAYS = {TripType.domestic: 15, TripType.international: 30}

NON_REFUNDABLE = {
    TripType.domestic: [
        "Air or rail tickets already issued (refunded only as per the carrier's rules)",
        "Booking service fee",
    ],
    TripType.international: [
        "Air tickets already issued (refunded only as per the airline's fare rules)",
        "Visa fees and visa service charges",
        "Travel insurance premium once the policy has been issued",
        "Booking service fee",
    ],
}

TCS_NOTE = ("Tax Collected at Source (TCS) is deposited against the traveller's PAN and is not refunded "
            "by Wayfinder Holidays; it is claimed as a credit in the traveller's income-tax return.")

CUSTOMER_CARE = "Customer Care: care@wayfinder-holidays.example, +91 44 4000 1234 (Mon-Sat 09:00-19:00 IST)"

SPECIAL_CASES_NOTE = ("Visa refusals, cancellations by Wayfinder Holidays, force majeure and medical cases are "
                      "handled only by Customer Care, which may apply different terms after checking documents.")


def tier_label(tiers: list[tuple[int, int]], index: int) -> str:
    """'45+ days', '30-44 days', … for the tier at `index`."""
    low = tiers[index][0]
    if index == 0:
        return f"{low}+ days"
    return f"{low}-{tiers[index - 1][0] - 1} days"


def charge_for(trip_type: TripType, fare_type: FareType, days_before: int) -> tuple[int, str]:
    """(cancellation charge %, tier label) for a cancellation `days_before` departure (>= 0)."""
    if fare_type == FareType.promo:
        return PROMO_CHARGE, "Promo fare (any time after booking)"
    if fare_type == FareType.flexi and days_before >= FLEXI_FREE_DAYS:
        return 0, f"Flexi fare, {FLEXI_FREE_DAYS}+ days"
    tiers = STANDARD_TIERS[trip_type]
    for i, (min_days, percent) in enumerate(tiers):
        if days_before >= min_days:
            prefix = (f"Flexi fare under {FLEXI_FREE_DAYS} days, standard tier "
                      if fare_type == FareType.flexi else "")
            return percent, prefix + tier_label(tiers, i)
    raise ValueError("days_before must be >= 0")
