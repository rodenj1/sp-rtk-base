# pyright: reportUnknownMemberType=false
# prometheus_client has incomplete type stubs
"""Prometheus metrics service for sp-base.

Maintains Prometheus Gauge/Counter objects and updates them from
``RelayStatus`` snapshots.  Unlike the relay package's MetricsCollector
(which needs direct access to live destination objects), this service
works entirely from the frozen ``RelayStatus`` dataclass that
``RelayService.get_status()`` returns.

The ``/metrics`` endpoint calls :meth:`update_from_status` on each
scrape so values are always fresh.
"""

from __future__ import annotations

import logging

from prometheus_client import CollectorRegistry, Gauge

from sp_rtk_base_relay.core.status import RelayStatus

logger = logging.getLogger(__name__)


class MetricsService:
    """Prometheus metrics backed by RelayStatus snapshots.

    Uses a **custom registry** so the default ``prometheus_client``
    global registry is left untouched (important for testability and
    avoiding duplicate-metric errors across service instances).

    Args:
        namespace: Prometheus metric name prefix.
        registry: Optional explicit registry.  If *None*, a new
            private registry is created.
    """

    def __init__(
        self,
        namespace: str = "sp_base",
        registry: CollectorRegistry | None = None,
    ) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        ns = namespace

        # ── Global gauges ─────────────────────────────────────────
        # Relay engine gauges use the sp_rtk_base_relay_* prefix
        # (the embedded relay package name), independent of this app's namespace.
        self.relay_running = Gauge(
            "sp_rtk_base_relay_running",
            "Relay engine running (1=running, 0=stopped)",
            registry=self.registry,
        )
        self.relay_uptime_seconds = Gauge(
            "sp_rtk_base_relay_uptime_seconds",
            "Relay engine uptime in seconds",
            registry=self.registry,
        )
        self.input_connected = Gauge(
            f"{ns}_input_connected",
            "Input source connection status (1=connected, 0=disconnected)",
            registry=self.registry,
        )
        self.input_bytes_received = Gauge(
            f"{ns}_input_bytes_received",
            "Total bytes received from input source",
            registry=self.registry,
        )
        self.input_seconds_since_last_data = Gauge(
            f"{ns}_input_seconds_since_last_data",
            "Seconds since last data from input source (-1 if no data yet)",
            registry=self.registry,
        )
        self.active_destinations = Gauge(
            f"{ns}_active_destinations",
            "Number of currently connected destinations",
            registry=self.registry,
        )
        self.total_destinations = Gauge(
            f"{ns}_total_destinations",
            "Total number of registered destinations",
            registry=self.registry,
        )
        self.chunks_distributed = Gauge(
            f"{ns}_chunks_distributed",
            "Total data chunks distributed to destinations",
            registry=self.registry,
        )
        self.frames_parsed = Gauge(
            f"{ns}_frames_parsed",
            "Total RTCM frames parsed",
            registry=self.registry,
        )

        # ── Per-destination gauges (labelled) ─────────────────────
        self.dest_connected = Gauge(
            f"{ns}_dest_connected",
            "Destination connection status (1=connected, 0=disconnected)",
            ["destination"],
            registry=self.registry,
        )
        self.dest_bytes_sent = Gauge(
            f"{ns}_dest_bytes_sent",
            "Total bytes sent to destination",
            ["destination"],
            registry=self.registry,
        )
        self.dest_messages_sent = Gauge(
            f"{ns}_dest_messages_sent",
            "Total messages sent to destination",
            ["destination"],
            registry=self.registry,
        )
        self.dest_messages_dropped = Gauge(
            f"{ns}_dest_messages_dropped",
            "Total messages dropped per destination",
            ["destination"],
            registry=self.registry,
        )
        self.dest_errors = Gauge(
            f"{ns}_dest_errors",
            "Total errors per destination",
            ["destination"],
            registry=self.registry,
        )
        self.dest_queue_depth = Gauge(
            f"{ns}_dest_queue_depth",
            "Current queue depth per destination",
            ["destination"],
            registry=self.registry,
        )

        logger.info("MetricsService initialized (namespace=%s)", ns)

    # ──────────────────────────────────────────────────────────────
    # Update methods
    # ──────────────────────────────────────────────────────────────

    def update_from_status(self, status: RelayStatus) -> None:
        """Refresh all Prometheus metrics from a RelayStatus snapshot.

        Args:
            status: Frozen RelayStatus from ``RelayService.get_status()``.
        """
        # Global
        self.relay_running.set(1 if status.running else 0)
        self.relay_uptime_seconds.set(status.uptime_seconds or 0.0)
        self.input_connected.set(1 if status.input.connected else 0)
        self.input_bytes_received.set(status.input.bytes_received)
        self.input_seconds_since_last_data.set(
            status.input.seconds_since_last_data
        )
        self.active_destinations.set(status.active_destination_count)
        self.total_destinations.set(status.total_destination_count)
        self.chunks_distributed.set(status.chunks_distributed)
        self.frames_parsed.set(status.frames_parsed)

        # Per-destination
        for dest in status.destinations:
            name = dest.name
            self.dest_connected.labels(destination=name).set(
                1 if dest.connected else 0
            )
            self.dest_bytes_sent.labels(destination=name).set(dest.bytes_sent)
            self.dest_messages_sent.labels(destination=name).set(
                dest.messages_sent
            )
            self.dest_messages_dropped.labels(destination=name).set(
                dest.messages_dropped
            )
            self.dest_errors.labels(destination=name).set(dest.errors)
            self.dest_queue_depth.labels(destination=name).set(
                dest.queue_depth
            )

    def update_idle(self) -> None:
        """Reset metrics to idle/stopped state.

        Called when the relay engine is not running so the ``/metrics``
        endpoint still returns valid Prometheus data with zeroed gauges.
        """
        self.relay_running.set(0)
        self.relay_uptime_seconds.set(0)
        self.input_connected.set(0)
        self.input_bytes_received.set(0)
        self.input_seconds_since_last_data.set(-1)
        self.active_destinations.set(0)
        self.total_destinations.set(0)
        self.chunks_distributed.set(0)
        self.frames_parsed.set(0)
