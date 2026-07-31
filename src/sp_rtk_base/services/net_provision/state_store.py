"""Durable clock bookkeeping for the provisioning supervisor (issue #9).

``seconds_disconnected`` and ``seconds_in_ap`` must survive a restart
of the provisioning service — a naive in-process counter resets on
every crash, ``Restart=on-failure``, or package upgrade, which breaks
two behaviors: a device already deep into its fallback window would
restart the window and never reach the AP, and a service restarting
more often than ``rescan_interval_seconds`` would never rescan, so the
AP would never come back down.

This module persists the timestamps the supervisor derives elapsed
time from — when an uplink was last seen, when the current AP session
started, and (issue #25) the consecutive-connect-failure count/SSID/
timestamp behind the failure-aware retry backoff — to a small JSON file
under a state directory. Nothing here talks to nmcli or drives the
loop; see ``supervisor.py`` for that.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("/var/lib/sp-rtk-base/net_provision_state.json")


class ProvisioningClockState(BaseModel):
    """The durable timestamps (and failure bookkeeping) elapsed-time
    calculations are built from.

    Wall-clock (``time.time()``) epoch seconds, or ``None`` when the
    event they mark has never happened.
    """

    last_uplink_at: float | None = None
    ap_started_at: float | None = None
    failed_connect_ssid: str | None = None
    consecutive_connect_failures: int = 0
    last_connect_failure_at: float | None = None


class ProvisioningStateStore:
    """Loads and saves :class:`ProvisioningClockState` to a JSON file.

    A missing or corrupt file is *not* the fail-loudly boundary the
    config loader is: it just means the supervisor starts with no
    memory of a prior disconnect or AP session, same as a fresh
    install. Losing this file loses precision, not safety — decide()
    still has sane behavior for "never seen an uplink" / "AP not
    active".
    """

    def __init__(self, path: Path = DEFAULT_STATE_PATH) -> None:
        self._path = path

    def load(self) -> ProvisioningClockState:
        """Read persisted state, defaulting to "no history" if unusable."""
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return ProvisioningClockState()
        except OSError:
            logger.warning(
                "Could not read provisioning state at %s — starting with no history",
                self._path,
                exc_info=True,
            )
            return ProvisioningClockState()
        try:
            return ProvisioningClockState.model_validate(json.loads(raw))
        except ValueError:
            logger.warning(
                "Discarding unreadable provisioning state at %s — starting with no "
                "history",
                self._path,
                exc_info=True,
            )
            return ProvisioningClockState()

    def save(self, state: ProvisioningClockState) -> None:
        """Atomically write ``state`` to disk (temp file + rename)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(f"{self._path.name}.tmp")
        tmp_path.write_text(json.dumps(state.model_dump()))
        os.replace(tmp_path, self._path)
