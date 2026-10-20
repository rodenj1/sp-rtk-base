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

from sp_rtk_base.services import (
    get_bluetooth_link_service,
    get_config_service,
    get_metrics_service,
    get_network_link_service,
    get_relay_service,
    get_signal_quality_service,
)
from sp_rtk_base.services.bluetooth_link.service import BluetoothLinkService
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.network_link.service import NetworkLinkService
from sp_rtk_base.services.relay_service import RelayService
from sp_rtk_base.services.signal_quality.service import SignalQualityService, View

logger = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# The Signal Quality view whose reading is exported: it gives the Relay's
# verdict while the Relay runs and survey-in's otherwise, so metrics cover
# both modes without a mode label.
METRICS_SIGNAL_VIEW: View = "survey"


@router.get("/metrics")
async def prometheus_metrics(
    relay: RelayService = Depends(get_relay_service),
    metrics: MetricsService = Depends(get_metrics_service),
    config_svc: ConfigService = Depends(get_config_service),
    signal_quality: SignalQualityService = Depends(get_signal_quality_service),
    bluetooth_link: BluetoothLinkService = Depends(get_bluetooth_link_service),
    network_link: NetworkLinkService = Depends(get_network_link_service),
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

    metrics.update_signal_quality(signal_quality.current(METRICS_SIGNAL_VIEW))
    metrics.update_bluetooth_link(bluetooth_link.current())
    metrics.update_network_links(network_link.current())

    output: bytes = generate_latest(metrics.registry)
    return Response(content=output, media_type=PROMETHEUS_CONTENT_TYPE)
