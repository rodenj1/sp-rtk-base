# pyright: reportUnknownMemberType=false
"""Prometheus metrics endpoint.

Serves ``/metrics`` in Prometheus exposition format. Metrics are
refreshed from the relay engine status on each scrape request.

When the relay is stopped, the endpoint still returns a 200 with
zeroed gauges so Prometheus sees the service as up but idle.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from prometheus_client import generate_latest

from sp_base.services import get_config_service, get_metrics_service, get_relay_service
from sp_base.services.config_service import ConfigService
from sp_base.services.metrics_service import MetricsService
from sp_base.services.relay_service import RelayService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics")
async def prometheus_metrics(
    relay: RelayService = Depends(get_relay_service),
    metrics: MetricsService = Depends(get_metrics_service),
    config_svc: ConfigService = Depends(get_config_service),
) -> Response:
    """Serve Prometheus metrics.

    Refreshes all metrics from the relay engine status snapshot,
    then returns the standard Prometheus exposition format.

    Returns 404 if metrics are disabled in application settings.

    Returns:
        200 response with Prometheus text format body, or 404 if disabled.
    """
    if not config_svc.get_settings().metrics_enabled:
        return JSONResponse(
            status_code=404,
            content={"detail": "Metrics are disabled"},
        )

    if relay.is_running:
        status = await relay.get_status()
        if status is not None:
            metrics.update_from_status(status)
        else:
            metrics.update_idle()
    else:
        metrics.update_idle()

    output: bytes = generate_latest(metrics.registry)
    return Response(content=output, media_type=PROMETHEUS_CONTENT_TYPE)
