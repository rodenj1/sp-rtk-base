"""MSM adapter: RTCM MSM Frames from the Relay → Signal Snapshots.

A receiver sends one MSM message per constellation per epoch.  The
multiple message bit (DF393) is 1 on every MSM of an epoch but the last,
so a Snapshot is closed on the MSM whose bit is 0.  If that Frame is
lost, the Snapshot is closed anyway when the next epoch visibly starts
(a message type repeats) or once it is :data:`EPOCH_TIMEOUT_SECONDS`
old.  The receiver has already applied its elevation mask to MSM, so
every signal here is one it puts in its corrections.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, cast

from pyrtcm import RTCMReader, parse_msm  # type: ignore[import-untyped]
from sp_rtk_base_relay import Frame

from sp_rtk_base.models.device_models import GnssConstellation
from sp_rtk_base.models.signal_quality_models import Band, Signal, SignalSnapshot

logger = logging.getLogger(__name__)

EPOCH_TIMEOUT_SECONDS = 1.5

# MSM message number without its last digit → constellation.  SBAS (110x)
# is deliberately absent, as in survey-in.
_CONSTELLATION: dict[int, GnssConstellation] = {
    107: GnssConstellation.GPS,
    108: GnssConstellation.GLONASS,
    109: GnssConstellation.GALILEO,
    111: GnssConstellation.QZSS,
    112: GnssConstellation.BEIDOU,
}
# MSM4–MSM7 carry C/N0 (DF403 or DF408); MSM1–3 do not.
_CNR_KINDS = {4, 5, 6, 7}


def is_msm(message_id: int) -> bool:
    """Whether *message_id* is an MSM this adapter can read C/N0 from."""
    return message_id // 10 in _CONSTELLATION and message_id % 10 in _CNR_KINDS


def band_for(constellation: GnssConstellation, rinex_code: str) -> Band:
    """Band group for an MSM cell's RINEX signal code, e.g. '1C', '2L', '7I'."""
    frequency = rinex_code[:1]
    if constellation is GnssConstellation.BEIDOU:
        return {"1": Band.L1, "2": Band.L1, "7": Band.L2}.get(frequency, Band.OTHER)
    return {"1": Band.L1, "2": Band.L2, "7": Band.L2}.get(frequency, Band.OTHER)


class MsmSnapshotAssembler:
    """Groups one epoch's MSM Frames into a Signal Snapshot."""

    def __init__(self) -> None:
        self._signals: list[Signal] = []
        self._messages: set[int] = set()
        self._started_at: float | None = None

    def add(self, frame: Frame, now: float) -> list[SignalSnapshot]:
        """Take in one Frame; return any Snapshots it completes (usually 0 or 1)."""
        if not is_msm(frame.message_id):
            return []
        decoded = self._decode(frame)
        if decoded is None:
            return []
        _constellation, signals, last_of_epoch = decoded

        completed: list[SignalSnapshot] = []
        stale = (
            self._started_at is not None
            and now - self._started_at > EPOCH_TIMEOUT_SECONDS
        )
        if self._messages and (stale or frame.message_id in self._messages):
            completed.append(self._close())  # the epoch's last Frame was lost

        if self._started_at is None:
            self._started_at = now
        self._signals.extend(signals)
        self._messages.add(frame.message_id)
        if last_of_epoch:
            completed.append(self._close())
        return completed

    def reset(self) -> None:
        """Drop any partly assembled epoch."""
        self._signals, self._messages, self._started_at = [], set(), None

    def _close(self) -> SignalSnapshot:
        snapshot = SignalSnapshot(
            captured_at=datetime.now(timezone.utc), signals=tuple(self._signals)
        )
        self.reset()
        return snapshot

    @staticmethod
    def _decode(
        frame: Frame,
    ) -> tuple[GnssConstellation, list[Signal], bool] | None:
        try:
            message: Any = RTCMReader.parse(frame.data)
            decoded = cast(
                "tuple[object, object, list[dict[str, Any]]]", parse_msm(message)
            )
            cells = decoded[2]
        except Exception:
            logger.debug("Undecodable MSM Frame %d", frame.message_id, exc_info=True)
            return None
        constellation = _CONSTELLATION[frame.message_id // 10]
        signals: list[Signal] = []
        for cell in cells:
            cn0 = cell.get("DF408", cell.get("DF403"))
            if cn0 is None or float(cn0) <= 0:
                continue
            signals.append(
                Signal(
                    constellation=constellation,
                    satellite=int(cell["CELLPRN"]),
                    band=band_for(constellation, str(cell["CELLSIG"])),
                    cn0_dbhz=float(cn0),
                )
            )
        return constellation, signals, int(getattr(message, "DF393", 0)) == 0
