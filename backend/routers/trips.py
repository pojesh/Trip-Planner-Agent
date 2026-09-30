import json
import logging
from typing import Iterator

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse

from backend.deps import get_plan_service
from backend.domain.models import (
    AddStopRequest, ChatRequest, SaveRequest, StopUpdate, Trip, TripRequest, TripSummary,
)
from backend.errors import AppError
from backend.services.plan_service import PlanService

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trips", tags=["trips"])


def ndjson(events: Iterator[dict]) -> StreamingResponse:
    """Stream progress events as NDJSON; errors become a final 'error' event."""
    def body():
        try:
            for event in events:
                yield json.dumps(event) + "\n"
        except AppError as e:
            yield json.dumps({"type": "error", **e.to_dict()}) + "\n"
        except Exception:
            log.exception("unexpected error while streaming")
            yield json.dumps({"type": "error", "code": "internal_error", "retryable": True,
                              "message": "Something went wrong on our side. Please retry.",
                              "provider": None}) + "\n"
    return StreamingResponse(body(), media_type="application/x-ndjson")


@router.post("/generate")
def generate(req: TripRequest, svc: PlanService = Depends(get_plan_service)):
    return ndjson(svc.generate(req))


@router.get("", response_model=list[TripSummary])
def list_trips(svc: PlanService = Depends(get_plan_service)):
    return svc.list_trips()


@router.get("/{trip_id}", response_model=Trip)
def get_trip(trip_id: str, svc: PlanService = Depends(get_plan_service)):
    return svc.get_trip(trip_id)


@router.post("/{trip_id}/save", response_model=Trip)
def save_trip(trip_id: str, req: SaveRequest, svc: PlanService = Depends(get_plan_service)):
    return svc.save_trip(trip_id, req.title)


@router.post("/{trip_id}/chat")
def chat(trip_id: str, req: ChatRequest, svc: PlanService = Depends(get_plan_service)):
    return ndjson(svc.chat(trip_id, req.message))


@router.patch("/{trip_id}/days/{day_id}/stops/{stop_id}")
def update_stop(trip_id: str, day_id: str, stop_id: str, upd: StopUpdate,
                svc: PlanService = Depends(get_plan_service)):
    return svc.update_stop(trip_id, day_id, stop_id, upd)


@router.delete("/{trip_id}/days/{day_id}/stops/{stop_id}")
def remove_stop(trip_id: str, day_id: str, stop_id: str, svc: PlanService = Depends(get_plan_service)):
    return svc.remove_stop(trip_id, day_id, stop_id)


@router.post("/{trip_id}/days/{day_id}/stops")
def add_stop(trip_id: str, day_id: str, req: AddStopRequest, svc: PlanService = Depends(get_plan_service)):
    return svc.add_stop(trip_id, day_id, req)


@router.post("/{trip_id}/days/{day_id}/recalculate")
def recalculate(trip_id: str, day_id: str, svc: PlanService = Depends(get_plan_service)):
    return svc.recalculate(trip_id, day_id)


@router.delete("/{trip_id}", status_code=204)
def delete_trip(trip_id: str, svc: PlanService = Depends(get_plan_service)):
    svc.delete_trip(trip_id)
    return Response(status_code=204)
