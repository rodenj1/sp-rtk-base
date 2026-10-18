"""The "Bluetooth link" row of the dashboard's Input Source card.

Shows the Bluetooth link strength verdict in the shared level colours,
with how far below comfortable a weak link is beneath it.  With no
reading it shows "—" and why.  The row never says "signal": that word
belongs to Signal Quality.
"""

from __future__ import annotations

from sp_rtk_base.models.bluetooth_link_models import (
    BluetoothLinkReading,
    BluetoothLinkVerdict,
)
from sp_rtk_base.services.bluetooth_link.monitor import WAITING_REASON
from sp_rtk_base.ui.components.level import LEVEL_COLOR
from sp_rtk_base.ui.components.status_card import status_metric

LABEL = "Bluetooth link"
TOOLTIP = (
    "How well this base station hears the Bluetooth module. "
    "Not satellite reception (see Signal Quality)."
)


def bluetooth_link_row(reading: BluetoothLinkReading | None) -> None:
    """Render the row for *reading* (``None`` before the first poll: waiting)."""
    if isinstance(reading, BluetoothLinkVerdict):
        status_metric(
            LABEL,
            reading.level,
            "bluetooth",
            subvalue=reading.subvalue,
            value_color=LEVEL_COLOR[reading.level],
            tooltip=TOOLTIP,
            testid="bluetooth-link-row",
        )
        return
    status_metric(
        LABEL,
        "—",
        "bluetooth",
        tooltip=reading.reason if reading is not None else WAITING_REASON,
        testid="bluetooth-link-row",
    )
