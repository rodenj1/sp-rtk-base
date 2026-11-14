"""The "Bluetooth link" row of the dashboard's Input Source card.

Shows the Bluetooth link strength verdict in the shared level colours,
with how far below comfortable a weak link is beneath it.  With no
reading it shows "—" and why.  The row never says "signal": that word
belongs to Signal Quality.

Built once and updated in place on each refresh (rtk_development#177).
"""

from __future__ import annotations

from sp_rtk_base.models.bluetooth_link_models import (
    BluetoothLinkReading,
    BluetoothLinkVerdict,
)
from sp_rtk_base.services.bluetooth_link.monitor import WAITING_REASON
from sp_rtk_base.ui.components.level import LEVEL_COLOR
from sp_rtk_base.ui.components.status_card import StatusMetric

LABEL = "Bluetooth link"
TOOLTIP = (
    "How well this base station hears the Bluetooth module. "
    "Not satellite reception (see Signal Quality)."
)


class BluetoothLinkRow:
    """The row for a Bluetooth link reading (``None``: waiting)."""

    def __init__(self, reading: BluetoothLinkReading | None) -> None:
        self._metric = StatusMetric(
            LABEL,
            "—",
            "bluetooth",
            tooltip=WAITING_REASON,
            testid="bluetooth-link-row",
        )
        self.update(reading)

    def update(self, reading: BluetoothLinkReading | None) -> None:
        """Show *reading* (``None`` before the first poll: waiting)."""
        if isinstance(reading, BluetoothLinkVerdict):
            self._metric.update(
                reading.level,
                reading.subvalue,
                value_color=LEVEL_COLOR[reading.level],
                tooltip=TOOLTIP,
            )
            return
        self._metric.update(
            "—",
            tooltip=reading.reason if reading is not None else WAITING_REASON,
        )

    def set_visibility(self, visible: bool) -> None:
        """Show or hide the row."""
        self._metric.set_visibility(visible)


def bluetooth_link_row(reading: BluetoothLinkReading | None) -> BluetoothLinkRow:
    """Render the row for *reading* (``None`` before the first poll: waiting)."""
    return BluetoothLinkRow(reading)
