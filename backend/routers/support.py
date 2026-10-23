from fastapi import APIRouter, Depends, Response

from backend.deps import get_support_service
from backend.domain.models import Booking, BookRequest, SupportChatRequest, Trip
from backend.routers.trips import ndjson
from backend.support.service import SupportService

router = APIRouter(prefix="/api", tags=["support"])


@router.post("/trips/{trip_id}/book", response_model=Trip)
def book_trip(trip_id: str, req: BookRequest, svc: SupportService = Depends(get_support_service)):
    return svc.book(trip_id, req.fare_type)


@router.get("/bookings", response_model=list[Booking])
def list_bookings(svc: SupportService = Depends(get_support_service)):
    return svc.list_bookings()


@router.post("/support/chat")
def support_chat(req: SupportChatRequest, svc: SupportService = Depends(get_support_service)):
    return ndjson(svc.chat(req.message, req.trip_id))


@router.post("/support/confirm/{quote_id}")
def confirm_cancellation(quote_id: str, svc: SupportService = Depends(get_support_service)):
    return svc.confirm_cancellation(quote_id)


@router.post("/support/keep/{quote_id}")
def keep_booking(quote_id: str, svc: SupportService = Depends(get_support_service)):
    return svc.keep_booking(quote_id)


@router.get("/support/history")
def history(svc: SupportService = Depends(get_support_service)):
    return svc.history()


@router.delete("/support/history", status_code=204)
def clear_history(svc: SupportService = Depends(get_support_service)):
    svc.clear_history()
    return Response(status_code=204)


@router.get("/support/traces")
def traces(limit: int = 20, svc: SupportService = Depends(get_support_service)):
    return svc.traces(min(max(limit, 1), 100))


@router.get("/support/index")
def index_stats(svc: SupportService = Depends(get_support_service)):
    return svc.index_stats()


@router.post("/support/reindex")
def reindex(svc: SupportService = Depends(get_support_service)):
    return svc.reindex()
