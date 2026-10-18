"""Bluetooth link strength models.

**Bluetooth link strength** is how well the base station's Bluetooth
adapter hears the module at the far end of a Bluetooth Input profile,
while connected.  Never "signal": that word belongs to Signal Quality.
See CONTEXT.md.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from sp_rtk_base.models.signal_quality_models import SignalLevel


class BluetoothLinkVerdict(BaseModel):
    """A judged Bluetooth link strength, from a window of readings."""

    model_config = ConfigDict(frozen=True)

    level: SignalLevel
    # The window's median reading, in dB relative to the controller's
    # comfortable range (0 = inside it, negative = that far below it).
    median_db: float

    @property
    def subvalue(self) -> str | None:
        """How far below comfortable the median is, or ``None`` when it isn't."""
        if self.median_db >= 0:
            return None
        return f"{-self.median_db:g} dB below comfortable"


class BluetoothLinkNoReading(BaseModel):
    """No Bluetooth link strength to show, and why, in words for the operator."""

    model_config = ConfigDict(frozen=True)

    reason: str


BluetoothLinkReading = BluetoothLinkVerdict | BluetoothLinkNoReading
