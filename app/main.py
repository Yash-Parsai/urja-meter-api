"""The HTTP API (FastAPI). Run it with:  uvicorn app.main:app"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .catalog import CatalogService, prune_tree, search_meters
from .config import Settings
from .errors import PortalAuthError, PortalError, PortalNotFound, PortalProtocolError, PortalUnavailable
from .models import (
    ErrorBody,
    Health,
    HierarchyNode,
    MeterDetail,
    MeterPage,
    ReadingsResponse,
    SessionStatus,
    Transformer,
    TransformerList,
)
from .normalize import IST, LEVELS, parse_readings
from .portal import PortalClient

log = logging.getLogger("urja.api")

DESCRIPTION = """
A clean, read-only API in front of the legacy **Urja Meter Ops** portal.

* Meter and transformer data come from the portal's bulk export and are cached (see `/health`).
* Readings are fetched live from the portal on every request.
* Timestamps are ISO 8601. The portal sends no timezone; **IST (+05:30) is assumed**.
* Errors always look like `{"error": "<code>", "message": "<text>"}`.
"""

ERRORS = {
    404: {"model": ErrorBody, "description": "Unknown meter, transformer or route."},
    422: {"model": ErrorBody, "description": "A parameter is missing or invalid."},
    502: {"model": ErrorBody, "description": "The portal rejected us or answered in an unexpected way."},
    503: {"model": ErrorBody, "description": "The portal could not be reached."},
}


class ApiError(Exception):
    def __init__(self, status_code: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.error, self.message = status_code, error, message


@dataclass
class Services:
    portal: PortalClient
    catalog: CatalogService


def create_app(settings: Settings | None = None, portal: PortalClient | None = None) -> FastAPI:
    """Build the app. Without arguments, settings come from the environment when the server starts.
    Tests pass their own `portal` (a client wired to a fake portal)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        active_settings = settings or (portal and portal_settings(portal)) or Settings.from_env()
        client = portal or PortalClient(active_settings)
        catalog = CatalogService(client, ttl_seconds=active_settings.catalog_ttl_seconds)
        app.state.services = Services(client, catalog)
        try:  # warm up so the first request is fast; a portal outage must not stop the service from starting
            await run_in_threadpool(catalog.snapshot)
        except PortalError as exc:
            log.error("could not load the catalog at startup (%s); will retry on the first request", exc)
        yield
        client.close()

    app = FastAPI(
        title="Urja Meter API",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "meters", "description": "Meter details, search and readings."},
            {"name": "network", "description": "Transformers and the network hierarchy."},
            {"name": "ops", "description": "Service health."},
        ],
    )
    app.state.services = None
    _add_error_handlers(app)
    _add_routes(app)
    return app


def portal_settings(portal: PortalClient) -> Settings:
    return portal._settings  # noqa: SLF001 - a client always carries the settings it was built with


# ---------------------------------------------------------------------- error handling
def _add_error_handlers(app: FastAPI) -> None:
    def envelope(status: int, error: str, message: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": error, "message": message})

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return envelope(exc.status_code, exc.error, exc.message)

    @app.exception_handler(PortalError)
    async def _portal_error(request: Request, exc: PortalError) -> JSONResponse:
        log.warning("portal problem on %s: %s", request.url.path, exc)
        if isinstance(exc, PortalNotFound):
            return envelope(404, "not_found", "The portal has no such resource.")
        if isinstance(exc, PortalUnavailable):
            return envelope(503, "portal_unavailable", "The Urja portal could not be reached. Try again shortly.")
        if isinstance(exc, PortalAuthError):
            return envelope(502, "portal_auth_failed", "This service could not authenticate with the Urja portal.")
        if isinstance(exc, PortalProtocolError):
            return envelope(502, "portal_unexpected_response", "The Urja portal answered in an unexpected way.")
        return envelope(502, "portal_error", "The Urja portal request failed.")

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"] if part != "query")
        return envelope(422, "invalid_request", f"{where}: {first['msg']}")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        return envelope(exc.status_code, codes.get(exc.status_code, "http_error"), str(exc.detail))


# ---------------------------------------------------------------------- routes
def get_services(request: Request) -> Services:
    services = request.app.state.services
    if services is None:  # only possible if the app is used without starting it
        raise ApiError(503, "not_started", "The service has not finished starting.")
    return services


def _add_routes(app: FastAPI) -> None:
    @app.get("/health", response_model=Health, tags=["ops"], summary="Service health")
    def health(services: Services = Depends(get_services)) -> Health:
        """Reports state only; it never calls the portal."""
        catalog = services.catalog.status()
        status = "starting" if not catalog.loaded else "degraded" if catalog.stale else "ok"
        return Health(status=status, session=SessionStatus(**services.portal.session_info()), catalog=catalog)

    @app.get("/meters", response_model=MeterPage, responses=ERRORS, tags=["meters"], summary="Search and list meters")
    def list_meters(
        q: str | None = Query(None, description="Text in the meter id or serial number (ignores case)."),
        status: str | None = Query(None, description="Installed, Faulty or Decommissioned."),
        make: str | None = Query(None, description="Manufacturer, e.g. HPL."),
        phase: str | None = Query(None, description="'single' or 'three'."),
        build: str | None = Query(None, description="'legacy' or 'v2'."),
        install_type: str | None = Query(None, description="'Whole Current' or 'CT Operated'."),
        transformer: str | None = Query(None, description="Transformer code, e.g. DT-001."),
        node: str | None = Query(None, description="A hierarchy path from /hierarchy, e.g. Z-01/C-01. Matches everything below it."),
        lat: float | None = Query(None, ge=-90, le=90, description="With lng: only meters near this point, closest first."),
        lng: float | None = Query(None, ge=-180, le=180),
        radius_km: float = Query(5.0, gt=0, le=500, description="Search radius when lat and lng are given."),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        services: Services = Depends(get_services),
    ) -> MeterPage:
        if (lat is None) != (lng is None):
            raise ApiError(422, "invalid_request", "lat and lng must be given together")
        near = (lat, lng, radius_km) if lat is not None and lng is not None else None
        matches = search_meters(
            services.catalog.snapshot(),
            q=q, status=status, make=make, phase=phase, build=build,
            install_type=install_type, transformer=transformer, node=node, near=near,
        )
        start = (page - 1) * page_size
        return MeterPage(items=matches[start : start + page_size], total=len(matches), page=page, page_size=page_size)

    @app.get("/meters/{meter_id}", response_model=MeterDetail, responses=ERRORS, tags=["meters"], summary="One meter, with hierarchy and location")
    def get_meter(meter_id: str, services: Services = Depends(get_services)) -> MeterDetail:
        return _find_meter(services, meter_id)

    @app.get("/meters/{meter_id}/readings", response_model=ReadingsResponse, responses=ERRORS, tags=["meters"], summary="Half-hourly readings, live from the portal")
    def get_readings(
        meter_id: str,
        from_: datetime | None = Query(None, alias="from", description="Only readings at or after this time. A time without an offset is read as IST."),
        to: datetime | None = Query(None, description="Only readings at or before this time."),
        services: Services = Depends(get_services),
    ) -> ReadingsResponse:
        """The portal holds about 7 days of half-hourly readings, currently ending 30 June 2026.
        `consumption_kwh` is the difference to the previous reading, also for the first reading inside a `from` filter."""
        meter = _find_meter(services, meter_id)
        start, end = _as_ist(from_), _as_ist(to)
        if start and end and start > end:
            raise ApiError(422, "invalid_request", "'from' must not be later than 'to'")
        readings, skipped = parse_readings(services.portal.read_energy(meter.meter_id))
        if skipped:
            log.warning("%s: skipped %d readings with an unusable timestamp", meter.meter_id, skipped)
        latest = readings[-1].timestamp if readings else None
        chosen = [r for r in readings if (start is None or r.timestamp >= start) and (end is None or r.timestamp <= end)]
        used = [r.consumption_kwh for r in chosen if r.consumption_kwh is not None]
        return ReadingsResponse(
            meter_id=meter.meter_id,
            timezone="+05:30 (IST, assumed)",
            latest_reading_at=latest,
            count=len(chosen),
            total_consumption_kwh=round(sum(used), 4) if used else None,
            readings=chosen,
        )

    @app.get("/transformers", response_model=TransformerList, responses=ERRORS, tags=["network"], summary="All distribution transformers")
    def list_transformers(services: Services = Depends(get_services)) -> TransformerList:
        items = list(services.catalog.snapshot().transformers)
        return TransformerList(items=items, total=len(items))

    @app.get("/transformers/{code}", response_model=Transformer, responses=ERRORS, tags=["network"], summary="One transformer")
    def get_transformer(code: str, services: Services = Depends(get_services)) -> Transformer:
        found = services.catalog.snapshot().transformers_by_code.get(code.strip().upper())
        if found is None:
            raise ApiError(404, "transformer_not_found", f"No transformer with code {code!r}.")
        return found

    @app.get("/hierarchy", response_model=list[HierarchyNode], responses=ERRORS, tags=["network"], summary="The network as a tree")
    def hierarchy(
        depth: int = Query(len(LEVELS), ge=1, le=len(LEVELS), description="How many levels to return (1 = zones only)."),
        services: Services = Depends(get_services),
    ) -> list[HierarchyNode]:
        """Zone, circle, division, subdivision, substation, feeder, transformer. Codes are only unique
        within their parent, so every node carries a full `path` that is unique."""
        return prune_tree(list(services.catalog.snapshot().tree), depth)


def _find_meter(services: Services, meter_id: str) -> MeterDetail:
    meter = services.catalog.lookup(meter_id)
    if meter is None:
        raise ApiError(404, "meter_not_found", f"No meter with id {meter_id!r}.")
    return meter


def _as_ist(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=IST) if value.tzinfo is None else value


app = create_app()
