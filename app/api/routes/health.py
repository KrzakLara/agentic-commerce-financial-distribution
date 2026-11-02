from datetime import UTC, datetime

from fastapi import APIRouter

from app import __version__
from app.api.deps import SettingsDep
from app.api.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness check",
    description="Returns HTTP 200 while the application process is up. "
    "Exposes no secrets and performs no external calls.",
)
def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        api_version=settings.api_version,
        environment=settings.environment.value,
        timestamp=datetime.now(UTC),
    )
