"""Console-script entry point for the network-provisioning supervisor.

Registered as ``sp-rtk-base-net-provision`` in ``pyproject.toml``,
driven by its own systemd unit that runs **independently** of
``sp-rtk-base.service`` (issue #6, story 17; issue #9) — the two must
not depend on each other so the provisioning loop keeps self-healing
the network's Ethernet/WiFi/AP state even while the web app is down
(or vice versa).

This module is wiring only: config loading, the durable clock store,
and the loop itself live in ``services/net_provision/``. Unlike
``cli/config_audit.py``, it is *not* excluded from coverage
measurement — extracting the signal-handler helper leaves a
straight-line ``main()`` that mocks cleanly, so it reaches the normal
coverage bar rather than needing the same relaxation.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from types import FrameType

from sp_rtk_base.services.net_provision import (
    NetProvisionConfigError,
    NmcliAdapter,
    Portal,
    ProvisioningStateStore,
    load_net_provision_config,
    run_forever,
)

logger = logging.getLogger(__name__)


def _install_shutdown_handler(stop_event: threading.Event) -> None:
    """Make SIGTERM/SIGINT set ``stop_event`` instead of killing the process.

    systemd sends SIGTERM on ``systemctl stop``/restart; without this,
    the loop would be killed mid-tick instead of exiting after its
    current iteration.
    """

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %s — stopping provisioning supervisor", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


def main() -> None:
    """Load config, wire the adapter/state store, and run the loop forever."""
    logging.basicConfig(level=logging.INFO)

    try:
        config = load_net_provision_config()
    except NetProvisionConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    adapter = NmcliAdapter(config)
    state_store = ProvisioningStateStore()
    portal = Portal(adapter=adapter, config=config)
    stop_event = threading.Event()
    _install_shutdown_handler(stop_event)

    def _sync_portal(ap_active: bool) -> None:
        if ap_active:
            portal.start()
        else:
            portal.stop()

    try:
        run_forever(
            adapter=adapter,
            config=config,
            state_store=state_store,
            stop_event=stop_event,
            on_ap_active=_sync_portal,
        )
    finally:
        # Belt-and-braces: leaves nothing bound to port 80/53 on shutdown
        # regardless of what AP state the last tick left the portal in.
        portal.stop()


if __name__ == "__main__":
    main()
