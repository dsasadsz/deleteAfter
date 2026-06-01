from fastapi import APIRouter, Request

from app.monitoring.metrics import runtime_metrics_snapshot

router = APIRouter(prefix="/api/metrics", tags=["monitoring"])


@router.get("/runtime")
def runtime_metrics(request: Request) -> dict:
    return runtime_metrics_snapshot(request.app)
