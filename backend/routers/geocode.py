from fastapi import APIRouter, Depends

from backend.deps import get_plan_service
from backend.domain.models import GeocodeRequest, PlaceResult
from backend.services.plan_service import PlanService

router = APIRouter(prefix="/api/geocode", tags=["geocode"])


@router.post("/search", response_model=list[PlaceResult])
def search(req: GeocodeRequest, svc: PlanService = Depends(get_plan_service)):
    return svc.geocode(req)
