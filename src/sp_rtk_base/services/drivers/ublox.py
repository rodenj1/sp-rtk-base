"""u-blox GPS receiver driver using PyUBX2.

Implements the GpsReceiverDriver ABC for u-blox Gen9+ receivers
(ZED-F9P, ZED-F9R, NEO-M9N, etc.) using the UBX binary protocol.

Configuration is done via CFG-VALSET/CFG-VALGET (Gen9+ config
database), and status is read via MON-VER and NAV-SVIN messages.
"""

from __future__ import annotations

import fcntl
import logging
import threading
import time
from typing import Literal

import serial  # type: ignore[import-untyped]
from pyubx2 import (  # type: ignore[import-untyped]
    POLL,
    SET,
    UBXMessage,
    UBXReader,
)

from sp_rtk_base.models.device_models import (
    ALL_RTCM_MESSAGE_IDS as _ALL_RTCM_IDS,
)
from sp_rtk_base.models.device_models import (
    BaseMode,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceInfo,
    DynModel,
    FixedBaseConfig,
    GnssConfig,
    GnssConstellation,
    GnssSystemConfig,
    GpsFixType,
    GpsPosition,
    PortId,
    PortProtocolConfig,
    ReceiverScalarConfig,
    RtcmOutputPort,
    RtcmPortConfig,
    RtcmRowId,
    SurveyInConfig,
    SurveyInProgress,
    UbxProtocol,
)
from sp_rtk_base.models.hardware_identity import resolve_hardware_identity
from sp_rtk_base.services.drivers.base import GpsReceiverDriver

logger = logging.getLogger(__name__)

# Default timeout for waiting for UBX responses (seconds)
_READ_TIMEOUT = 3.0

# Max read iterations when waiting for a specific UBX response.
# Needs to be high enough to skip interleaved RTCM/NAV messages
# on a busy receiver (base station mode streams many RTCM frames).
_MAX_READ_ATTEMPTS = 50

# u-blox output port suffixes for CFG key names
_RTCM_PORTS: list[str] = [p.value for p in RtcmOutputPort]

# RTCM row IDs and their CFG key base names.
# Every row follows pattern CFG_MSGOUT_RTCM_3X_TYPE{id}_{port} — 4072.0
# and 4072.1 are distinct rows with distinct CFG keys.
_RTCM_KEY_BASES: dict[RtcmRowId, str] = {
    RtcmRowId.RTCM_1005: "CFG_MSGOUT_RTCM_3X_TYPE1005",
    RtcmRowId.RTCM_4072_0: "CFG_MSGOUT_RTCM_3X_TYPE4072_0",
    RtcmRowId.RTCM_4072_1: "CFG_MSGOUT_RTCM_3X_TYPE4072_1",
    RtcmRowId.RTCM_1074: "CFG_MSGOUT_RTCM_3X_TYPE1074",
    RtcmRowId.RTCM_1077: "CFG_MSGOUT_RTCM_3X_TYPE1077",
    RtcmRowId.RTCM_1084: "CFG_MSGOUT_RTCM_3X_TYPE1084",
    RtcmRowId.RTCM_1087: "CFG_MSGOUT_RTCM_3X_TYPE1087",
    RtcmRowId.RTCM_1094: "CFG_MSGOUT_RTCM_3X_TYPE1094",
    RtcmRowId.RTCM_1097: "CFG_MSGOUT_RTCM_3X_TYPE1097",
    RtcmRowId.RTCM_1124: "CFG_MSGOUT_RTCM_3X_TYPE1124",
    RtcmRowId.RTCM_1127: "CFG_MSGOUT_RTCM_3X_TYPE1127",
    RtcmRowId.RTCM_1230: "CFG_MSGOUT_RTCM_3X_TYPE1230",
}


def _rtcm_key(msg_id: RtcmRowId, port: str) -> str:
    """Build the full CFG key name for an RTCM row + port."""
    return f"{_RTCM_KEY_BASES[msg_id]}_{port}"


# CFG key prefix for each port covered by the live port-protocol read
# (issue #57) — UART1/UART2 use a numbered prefix, USB does not.
_PROTOCOL_PORT_PREFIXES: dict[PortId, str] = {
    PortId.UART1: "CFG_UART1",
    PortId.UART2: "CFG_UART2",
    PortId.USB: "CFG_USB",
}

# Explicitly typed so a `for direction in _PROTOCOL_DIRECTIONS` loop
# variable narrows to ``Literal["IN", "OUT"]`` under mypy strict, not
# just pyright — an inline `("IN", "OUT")` tuple loses that narrowing
# under mypy.
_PROTOCOL_DIRECTIONS: tuple[Literal["IN", "OUT"], ...] = ("IN", "OUT")


def _protocol_key(
    port: PortId, direction: Literal["IN", "OUT"], protocol: UbxProtocol
) -> str:
    """Build the CFG_{PORT}{IN,OUT}PROT_{PROTOCOL} key name.

    Args:
        port: Communication port.
        direction: ``"IN"`` or ``"OUT"``.
        protocol: Protocol to query.
    """
    return f"{_PROTOCOL_PORT_PREFIXES[port]}{direction}PROT_{protocol.value}"


# ---------------------------------------------------------------------------
# Apply-config primitives (issue #61)
# ---------------------------------------------------------------------------

# u-blox CFG_NAVSPG_DYNMODEL values. 1 is reserved/unused on the wire.
_DYN_MODEL_VALUES: dict[DynModel, int] = {
    DynModel.PORTABLE: 0,
    DynModel.STATIONARY: 2,
    DynModel.PEDESTRIAN: 3,
    DynModel.AUTOMOTIVE: 4,
    DynModel.SEA: 5,
    DynModel.AIRBORNE_1G: 6,
    DynModel.AIRBORNE_2G: 7,
    DynModel.AIRBORNE_4G: 8,
}

# Reverse of ``_DYN_MODEL_VALUES`` — for ``get_dyn_model``'s read-back.
_DYN_MODEL_NAMES: dict[int, DynModel] = {v: k for k, v in _DYN_MODEL_VALUES.items()}

# u-blox CFG_TMODE_MODE values — same mapping ``_parse_cfg_tmode`` reads back.
_TMODE_MODE_VALUES: dict[BaseMode, int] = {
    BaseMode.DISABLED: 0,
    BaseMode.SURVEY_IN: 1,
    BaseMode.FIXED: 2,
}

# Reverse of ``_TMODE_MODE_VALUES`` — for ``get_receiver_scalars``'s
# read-back and ``_parse_cfg_tmode``.
_TMODE_MODE_NAMES: dict[int, BaseMode] = {v: k for k, v in _TMODE_MODE_VALUES.items()}

# The six per-constellation enable keys (issue #104) — CFG-GNSS SET is
# deprecated on the F9 config interface in favour of these. GLONASS,
# Galileo and BeiDou use their three-letter IDs; the rest match the
# ``GnssConstellation`` member name. IMES and NavIC are deliberately
# absent — they have no ``GnssConstellation`` member, so there is no
# key here for a write to ever touch.
_GNSS_SIGNAL_ENA_KEYS: dict[GnssConstellation, str] = {
    GnssConstellation.GPS: "CFG_SIGNAL_GPS_ENA",
    GnssConstellation.GLONASS: "CFG_SIGNAL_GLO_ENA",
    GnssConstellation.GALILEO: "CFG_SIGNAL_GAL_ENA",
    GnssConstellation.BEIDOU: "CFG_SIGNAL_BDS_ENA",
    GnssConstellation.SBAS: "CFG_SIGNAL_SBAS_ENA",
    GnssConstellation.QZSS: "CFG_SIGNAL_QZSS_ENA",
}

# The scalar CFG keys ``get_receiver_scalars`` polls in one CFG-VALGET
# round trip (issue #97) — 14 keys (8 original + the 6 constellation
# enable keys added by issue #104), well under the 64-key poll cap.
_RECEIVER_SCALAR_KEYS: list[str] = [
    "CFG_UART1_BAUDRATE",
    "CFG_UART2_BAUDRATE",
    "CFG_RATE_MEAS",
    "CFG_NAVSPG_DYNMODEL",
    "CFG_TMODE_MODE",
    "CFG_NAVSPG_INFIL_MINELEV",
    "CFG_SIGNAL_BDS_B2_ENA",
    "CFG_SPI_ENABLED",
    *_GNSS_SIGNAL_ENA_KEYS.values(),
]

# Ports the RTCM matrix write covers — deliberately excludes I2C/SPI,
# which the profile schema doesn't claim (see RtcmStreamConfig).
_MATRIX_PORTS: tuple[PortId, ...] = (PortId.UART1, PortId.UART2, PortId.USB)


class UbloxDriver(GpsReceiverDriver):
    """u-blox GPS receiver driver using UBX protocol via PyUBX2.

    Supports Gen9+ receivers (ZED-F9P, etc.) with the CFG-VALSET/
    CFG-VALGET configuration interface.
    """

    # Default wall-clock timeout for connect (MON-VER poll) in seconds
    CONNECT_TIMEOUT = 10.0

    def __init__(self) -> None:
        self._serial: serial.Serial | None = None  # type: ignore[no-any-unimported]
        self._reader: UBXReader | None = None  # type: ignore[no-any-unimported]
        self._device_info: DeviceInfo | None = None
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        # Last-known port/baud — captured on connect so
        # ``reset_and_reconnect()`` can reopen the same port after
        # the hardware reset re-enumerates the USB.
        self._port: str | None = None
        self._baud_rate: int | None = None
        # Advisories queued by ``_write_and_verify_locked`` for a flash
        # divergence (issue #103) — drained by ``drain_warnings``.
        self._warnings: list[str] = []

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def vendor_name(self) -> str:
        return "u-blox"

    def get_capabilities(self) -> set[DeviceCapability]:
        return {
            DeviceCapability.SURVEY_IN,
            DeviceCapability.FIXED_BASE,
            DeviceCapability.RTCM_MESSAGE_SELECT,
            DeviceCapability.SAVE_TO_FLASH,
            DeviceCapability.POSITION_STREAM,
            DeviceCapability.SATELLITE_INFO,
            DeviceCapability.GNSS_SELECT,
        }

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def cancel_connect(self) -> None:
        """Cancel an in-progress connect attempt.

        Sets the cancel event and closes the serial port, which forces
        any blocking ``reader.read()`` to fail immediately.
        """
        self._cancel_event.set()
        # Force-close serial port to unblock reader.read()
        if self._serial is not None:
            try:
                if self._serial.is_open:
                    self._serial.close()
            except Exception:
                pass
        logger.info("Connect cancelled by user")

    def connect(self, port: str, baud_rate: int = 115200) -> DeviceInfo:
        if self._serial is not None and self._serial.is_open:
            raise ConnectionError("Already connected — disconnect first")

        self._cancel_event.clear()
        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baud_rate,
                timeout=_READ_TIMEOUT,
                exclusive=True,  # TIOCEXCL — kernel prevents other opens
            )
            # Advisory lock — gives a clear error if another process sneaks in
            try:
                fcntl.flock(self._serial.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as lock_err:
                self._cleanup()
                raise ConnectionError(
                    f"Serial port {port} is locked by another process"
                ) from lock_err

            self._reader = UBXReader(
                self._serial,
                protfilter=7,  # NMEA + UBX + RTCM3
                quitonerror=0,  # ERR_IGNORE — suppress console noise from corrupt frames
            )

            # Read device identity via MON-VER
            info = self._poll_mon_ver()
            self._device_info = info
            # Remember for reset_and_reconnect().
            self._port = port
            self._baud_rate = baud_rate
            logger.info(
                "Connected to u-blox %s (FW %s) on %s @ %d",
                info.model,
                info.firmware_version,
                port,
                baud_rate,
            )
            return info

        except serial.SerialException as exc:
            self._cleanup()
            raise ConnectionError(f"Failed to open {port}: {exc}") from exc
        except Exception as exc:
            self._cleanup()
            raise ConnectionError(f"Connection failed: {exc}") from exc

    def disconnect(self) -> None:
        self._cleanup()
        self._device_info = None
        logger.info("u-blox disconnected")

    def reset_and_reconnect(self) -> DeviceInfo:
        """Hardware-reset the receiver and reconnect on the same port.

        This is the only software-issuable way to clear the
        BBR-backed NAV-SVIN ``dur`` accumulator on ZED-F9P firmware
        HPG 1.12.  Six controlled-software-reset variants
        (resetMode 1, 2, 8, 9 with various BBR-bit masks including
        a full coldstart-equivalent) were verified empirically to
        leave ``dur`` unchanged; only ``resetMode=0`` (hardware
        reset immediate) actually resets it.

        Sequence:
          1. Write ``CFG_TMODE_MODE=0`` to layer=7 (RAM+BBR+Flash)
             so the post-reset boot lands in rover mode regardless
             of what state was last persisted.  Saved fixed-base
             coordinates (LAT/LON/HEIGHT/ECEF keys) remain in Flash
             — only the MODE key is touched, so the operator can
             still Restore back to a fixed base from the UI.
          2. Send UBX-CFG-RST with ``resetMode=0x00`` and the
             ``pos`` BBR bit set.  This triggers an immediate chip
             reset and causes the USB serial port to re-enumerate.
          3. Close the now-stale serial handle.
          4. Sleep ``_HARDWARE_RESET_SETTLE_S`` for the chip and
             host's USB stack to settle.
          5. Reopen the serial port on the same path/baud and
             redo the MON-VER handshake.

        Returns:
            Refreshed ``DeviceInfo`` from the reconnected receiver.

        Raises:
            RuntimeError: If the driver was never connected (no
                saved port/baud).
            ConnectionError: If the receiver fails to come back
                within ``CONNECT_TIMEOUT`` after the reset.
        """
        if self._port is None or self._baud_rate is None:
            raise RuntimeError(
                "Cannot reset — driver was never connected "
                "(no saved port/baud to reopen)."
            )
        port = self._port
        baud_rate = self._baud_rate

        with self._lock:
            # 1. Pin Flash to TMODE_MODE=0 so the post-reset boot
            # is rover mode regardless of prior state.
            self._send_cfg_valset_locked(
                [("CFG_TMODE_MODE", 0)], layer=self._TMODE_DISABLE_ALL_LAYERS
            )
            # 2. Hardware reset (drops USB).
            ser, _ = self._require_connection()
            msg = UBXMessage(  # type: ignore[misc]
                "CFG",
                "CFG-RST",
                SET,
                pos=1,
                resetMode=0x00,
            )
            ser.reset_input_buffer()
            ser.write(msg.serialize())  # type: ignore[union-attr]
            # 3. Close our handle — the OS will mark it disconnected
            # within ~100ms but we want to free it ASAP so the
            # post-reset reopen succeeds cleanly.
            self._cleanup()

        # 4. Wait for chip re-enumeration outside the lock so
        # concurrent poll calls fail fast (with "Not connected")
        # instead of blocking for the full settle window.
        time.sleep(self._HARDWARE_RESET_SETTLE_S)

        # 5. Reopen on the same port/baud and redo MON-VER.
        info = self.connect(port, baud_rate)
        logger.info(
            "Hardware reset + reconnect complete on %s @ %d",
            port,
            baud_rate,
        )
        return info

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def _cleanup(self) -> None:
        """Close serial port and reset internal state."""
        if self._serial is not None:
            try:
                if self._serial.is_open:
                    self._serial.close()
            except Exception:
                pass
        self._serial = None
        self._reader = None

    def _require_connection(self) -> tuple[serial.Serial, UBXReader]:  # type: ignore[no-any-unimported]
        """Return serial + reader, raising if not connected."""
        if self._serial is None or not self._serial.is_open or self._reader is None:
            raise ConnectionError("Not connected to device")
        return self._serial, self._reader

    # ------------------------------------------------------------------
    # Base station configuration
    # ------------------------------------------------------------------

    # How long to wait between the TMODE-disable and TMODE-enable
    # phases of ``configure_survey_in``.  Empirically the ZED-F9P
    # needs ~200-400 ms of quiet time after a TMODE-disable VALSET
    # before it will accept a re-enable as a *new* survey-in (rather
    # than silently latching the cached prior survey result).
    # 500 ms is conservative for all known ZED-F9P/F9R firmwares.
    _TMODE_RESTART_DELAY_S: float = 0.5

    # Gap between the two NAV-SVIN polls used to confirm the survey
    # has actually started by observing ``dur`` increment.  Must be
    # >= ~1 s because NAV-SVIN is emitted at 1 Hz and ``dur`` is a
    # whole-second counter.  2 s gives a 1-2 tick delta with margin.
    _SVIN_DUR_VERIFY_GAP_S: float = 2.0

    # CFG-VALSET layer bitmask.  Layer 1=RAM, 2=BBR, 4=Flash.
    # Per u-blox's own "F9P Base Survey in disable.txt" reference
    # script in the C099 board package, a clean TMODE-disable writes
    # to all three layers (1|2|4 = 7) so any TMODE-related config
    # from a prior session is also wiped.  Writing only to RAM leaves
    # BBR pinned at TMODE=1 across host restarts.
    _TMODE_DISABLE_ALL_LAYERS: int = 7

    # CFG-VALGET poll-layer enum — NOT the CFG-VALSET bitmask above.
    # u-blox defines this field as 0=RAM, 1=BBR, 2=Flash, 7=Default,
    # a plain enum rather than an OR-able bitmask. Default for
    # ``_read_cfg_keys_locked`` (issue #94): every existing caller
    # wants RAM, matching this method's pre-#94 hardcoded behaviour.
    _CFG_LAYER_RAM: int = 0
    _CFG_LAYER_FLASH: int = 2

    # The CFG-VALSET bitmask's Flash bit (see the layer comment above:
    # 1=RAM, 2=BBR, 4=Flash). Gates the flash read-back in
    # ``_write_and_verify_locked`` (issue #103) — a write that never
    # targets flash has nothing durable to check.
    _CFG_VALSET_FLASH_BIT: int = 4

    # How long to wait after a UBX-CFG-RST resetMode=0 (hardware
    # reset) for the chip to re-enumerate on USB and be ready to
    # accept a fresh serial connection.  Empirically ~2-3 s.  5 s
    # gives margin; the host's USB stack and pyserial settle in
    # this window.
    _HARDWARE_RESET_SETTLE_S: float = 5.0

    # Maximum ``NAV-SVIN.dur`` allowed at the first verify poll for
    # a survey-in start to be considered "fresh".  ``dur`` is a
    # BBR-backed accumulator on the ZED-F9P; if the CFG-RST didn't
    # actually clear it (a hardware-level failure we cannot fix from
    # software), the floor check fires a clear, actionable error
    # instead of letting the UI display a stale 17-hour duration.
    _SVIN_DUR_FLOOR_S: int = 30

    def configure_survey_in(self, config: SurveyInConfig) -> None:
        # Pre-reset check: the BBR-backed NAV-SVIN.dur accumulator
        # is NOT cleared by CFG-VALSET writes — only a CFG-RST=0
        # hardware reset clears it.  After a successful survey-in,
        # dur sits at >= 120s (the min-duration value at completion);
        # a fresh Start would fail our verify floor.  Detect that
        # case here and auto-reset transparently so the operator
        # doesn't see "stale accumulator" errors on the rerun path.
        #
        # The reset takes ~5-8s; we deliberately do not gate this on
        # a lock — ``reset_and_reconnect`` acquires its own lock
        # internally for the write phase and releases it for the
        # sleep-then-reconnect tail.
        with self._lock:
            baseline = self._get_survey_in_locked()
        if baseline.duration_seconds >= self._SVIN_DUR_FLOOR_S:
            logger.info(
                "Pre-survey reset: NAV-SVIN.dur=%ds in BBR from prior "
                "session — issuing hardware reset before fresh start",
                baseline.duration_seconds,
            )
            self.reset_and_reconnect()

        # All CFG-VALSET writers must hold self._lock so they cannot
        # interleave on the wire with a concurrent NAV/CFG/MON poll
        # from the same driver instance (e.g. the 2 s survey-in UI
        # poll timer firing the moment Start/Cancel is clicked).  An
        # interleaved write produces a corrupted UBX frame that the
        # receiver silently drops — no ACK is ever sent, and the
        # CFG-VALSET appears to "succeed" from the operator's POV
        # while having no effect.  See memory-bank/progress.md
        # 2026-05-27 "Cancel Survey-In doesn't cancel" entry.
        with self._lock:
            # Step 1: full-layer TMODE disable.  Per u-blox's own C099
            # "F9P Base Survey in disable.txt" script, the canonical
            # disable writes to all three layers (1|2|4 = 7) so any
            # TMODE config from a prior session is wiped consistently.
            # Flashed ECEF/LLH coordinates from a completed prior
            # survey persist — only the MODE key is touched, so the
            # operator can still switch back to a known fixed-base
            # position manually via Restore.
            #
            # Note: this does NOT reset the BBR-backed NAV-SVIN.dur
            # accumulator.  On HPG 1.12 only a hardware reset
            # (``reset_and_reconnect``) clears that counter.  The
            # dur-floor check below catches stale state and tells
            # the operator to use Reset GPS.
            self._send_cfg_valset_locked(
                [("CFG_TMODE_MODE", 0)], layer=self._TMODE_DISABLE_ALL_LAYERS
            )

            # Step 2: settle.  The ZED-F9P needs a brief quiet period
            # between TMODE-disable and TMODE-enable VALSETs so the
            # 0 -> 1 edge is registered as a fresh survey-in request
            # rather than coalesced with the previous state.
            time.sleep(self._TMODE_RESTART_DELAY_S)

            # Step 3: write the new survey-in parameters and enable.
            # Issue #42: this used to write RAM only (layer=1) on the
            # theory that survey-in is transient state; in practice
            # that meant the *enable* never survived a reboot or even
            # the app re-opening the port, silently reverting the base
            # to disabled. Write RAM+Flash (layer=5) instead, matching
            # ``configure_fixed_base`` — the transition intent must be
            # durable even though CFG_TMODE_MODE=1 is conceptually
            # transient while the survey itself is running.
            # CFG_TMODE_SVIN_ACC_LIMIT is in 0.1 mm units on the
            # wire (u-blox spec: "1 m = 10000, 3.2598 m = 32598").
            # The Python API uses mm, so multiply by 10 here.  Same
            # convention applies to ``CFG_TMODE_FIXED_POS_ACC`` in
            # ``configure_fixed_base``.  Mismatch was responsible
            # for the v0.3.8-and-earlier "survey never completes
            # even though displayed accuracy < target" symptom: we
            # were sending 50000 raw thinking it meant 50000 mm,
            # but the receiver read it as 5000 mm and held the
            # survey open until NAV-SVIN.meanAcc dropped below
            # 5000 mm (which it wasn't reaching at the bad-antenna
            # location).
            cfg_data = [
                ("CFG_TMODE_SVIN_MIN_DUR", config.min_duration_seconds),
                ("CFG_TMODE_SVIN_ACC_LIMIT", config.accuracy_limit_mm * 10),
                # Survey-in mode (last so params land first)
                ("CFG_TMODE_MODE", 1),
            ]
            # Verify-after-write: an ACK'd layer=5 write that silently
            # didn't persist is exactly issue #42's original failure
            # mode, and it wouldn't be caught by the dur-progression
            # check below (that confirms the survey *engine* started,
            # not that these CFG keys landed in flash).
            self._write_and_verify_locked(cfg_data, layer=5, label="Survey-in config")

            # Step 5: confirm a *fresh* survey is running.  Two
            # signals together:
            #   (a) ``before.duration_seconds < _SVIN_DUR_FLOOR_S``
            #       proves the CFG-RST actually reset the accumulator
            #       (a "true pass" — not a stale 17-hour value still
            #       ticking from a prior session).
            #   (b) ``after.dur > before.dur`` proves the survey-in
            #       state machine engaged after our TMODE=1 write.
            # NAV-SVIN.active is deliberately NOT checked — that flag
            # stays False on HPG 1.12 even when the receiver is
            # surveying.  Multiple u-blox forum threads document the
            # bug; no release notes claim a fix.
            before = self._get_survey_in_locked()
            time.sleep(self._SVIN_DUR_VERIFY_GAP_S)
            after = self._get_survey_in_locked()

            stale_accumulator = before.duration_seconds >= self._SVIN_DUR_FLOOR_S
            not_progressing = not (
                after.duration_seconds > before.duration_seconds
                and after.duration_seconds > 0
                and after.observations > 0
            )

            if stale_accumulator or not_progressing:
                # Roll back so a failed start doesn't leave the
                # receiver in TMODE=1 with phantom-survey state.
                try:
                    self._send_cfg_valset_locked(
                        [("CFG_TMODE_MODE", 0)],
                        layer=self._TMODE_DISABLE_ALL_LAYERS,
                    )
                except Exception:
                    logger.exception(
                        "Failed to roll back TMODE after survey-in start "
                        "failure — receiver may be in inconsistent state"
                    )

                if stale_accumulator:
                    raise RuntimeError(
                        "Survey-in start failed: the receiver has a "
                        f"stale survey-in accumulator (dur={before.duration_seconds}s, "
                        f"expected < {self._SVIN_DUR_FLOOR_S}s).  This "
                        "is the BBR-backed NAV-SVIN counter from a "
                        "previous session — CFG-VALSET cannot clear "
                        "it on ZED-F9P firmware HPG 1.12.  Click "
                        "'Reset GPS' to issue a hardware reset (the "
                        "only software-issuable way to clear this "
                        "state), then try Start Survey-In again.  "
                        "TMODE has been reset to 0."
                    )
                raise RuntimeError(
                    "Survey-in start failed: NAV-SVIN.dur did not "
                    f"advance over {self._SVIN_DUR_VERIFY_GAP_S:.0f}s "
                    f"(before: dur={before.duration_seconds}s "
                    f"obs={before.observations}; after: "
                    f"dur={after.duration_seconds}s "
                    f"obs={after.observations}).  The receiver "
                    "accepted the configuration but the survey-in "
                    "state machine did not engage.  TMODE has been "
                    "reset to 0."
                )

            # Issue #63: this used to force-apply an RTCM-only UART1/
            # UART2 output profile and stationary dynamics here on
            # every transition (issues #40/#38). That made this method
            # a competing writer that would silently overwrite an
            # operator-applied ``ReceiverConfig`` profile the next time
            # a survey-in started. Those invariants now live only in
            # the built-in profile and are surfaced as a non-blocking
            # pre-flight warning (``DeviceService.check_base_invariants``)
            # instead of being force-written here.
        logger.info(
            "Survey-in configured: %ds min, %dmm accuracy",
            config.min_duration_seconds,
            config.accuracy_limit_mm,
        )

    def disable_base_mode(self) -> None:
        """Disable TMODE on the receiver (CFG_TMODE_MODE=0).

        Used to cancel an in-progress survey-in or clear a fixed-base
        configuration.  Applied to RAM only — call ``save_to_flash()``
        afterwards if the change should persist.

        Verify-and-retry semantics: after the CFG-VALSET ACK is
        received, this method polls NAV-SVIN once to confirm
        ``active=False``.  If the survey is still active (i.e. the
        ACK was for a frame that the receiver discarded due to wire
        corruption, or the receiver simply ignored the write), the
        VALSET is retried once.  If the survey is *still* active
        after the second attempt, a ``RuntimeError`` is raised so
        the caller can surface the failure to the operator instead
        of silently leaving the survey running.

        Deliberately leaves ``CFG_NAVSPG_DYNMODEL`` untouched (issue
        #38): rover mode is not a concern of this app, so there's no
        "correct" dynamics class to restore here, and clearing it would
        regress a receiver that a prior fixed-base/survey-in session
        already set to stationary back to portable for no benefit.
        """
        with self._lock:
            self._send_cfg_valset_locked([("CFG_TMODE_MODE", 0)], layer=1)
            progress = self._get_survey_in_locked()
            if progress.active:
                logger.warning(
                    "TMODE=0 did not take effect on first attempt "
                    "(survey still active after %ds) — retrying",
                    progress.duration_seconds,
                )
                self._send_cfg_valset_locked([("CFG_TMODE_MODE", 0)], layer=1)
                progress = self._get_survey_in_locked()
                if progress.active:
                    raise RuntimeError(
                        "Cancel did not take effect: receiver still "
                        f"reports survey-in active after {progress.duration_seconds}s. "
                        "Try disconnecting and reconnecting, or power-cycle "
                        "the receiver."
                    )
        logger.info("Base mode disabled (TMODE=0)")

    def configure_fixed_base(self, config: FixedBaseConfig) -> None:
        # u-blox uses degrees * 1e-7 for lat/lon in integer form
        lat_hp = int(config.latitude * 1e7)
        lon_hp = int(config.longitude * 1e7)
        alt_cm = int(config.altitude_m * 100)

        # The ZED-F9P base engine will not generate RTCM corrections
        # unless a valid (non-origin) 3D position is present in ECEF —
        # writing LLH alone leaves CFG_TMODE_ECEF_X/Y/Z at their 0,0,0
        # default and the base silently never engages, even though
        # TMODE_MODE=2 and the RTCM message selection both ACK cleanly.
        # Derive ECEF from the same WGS84 LLH input so both
        # representations agree.
        ecef_x_m, ecef_y_m, ecef_z_m = self._llh_to_ecef(
            config.latitude, config.longitude, config.altitude_m
        )
        ecef_x_cm, ecef_x_hp = self._m_to_cm_hp(ecef_x_m)
        ecef_y_cm, ecef_y_hp = self._m_to_cm_hp(ecef_y_m)
        ecef_z_cm, ecef_z_hp = self._m_to_cm_hp(ecef_z_m)

        cfg_data = [
            ("CFG_TMODE_MODE", 2),  # Fixed mode
            ("CFG_TMODE_POS_TYPE", 1),  # LLH
            ("CFG_TMODE_LAT", lat_hp),
            ("CFG_TMODE_LON", lon_hp),
            ("CFG_TMODE_HEIGHT", alt_cm),
            # CFG_TMODE_FIXED_POS_ACC is in 0.1 mm units on the
            # wire (same convention as CFG_TMODE_SVIN_ACC_LIMIT).
            # The Python API uses mm, so multiply by 10 here.
            ("CFG_TMODE_FIXED_POS_ACC", config.accuracy_mm * 10),
            ("CFG_TMODE_ECEF_X", ecef_x_cm),
            ("CFG_TMODE_ECEF_Y", ecef_y_cm),
            ("CFG_TMODE_ECEF_Z", ecef_z_cm),
            ("CFG_TMODE_ECEF_X_HP", ecef_x_hp),
            ("CFG_TMODE_ECEF_Y_HP", ecef_y_hp),
            ("CFG_TMODE_ECEF_Z_HP", ecef_z_hp),
        ]
        with self._lock:
            # Pre-disable TMODE before writing the new fixed-base
            # config.  Without this, on a receiver currently in
            # TMODE=1 (survey-in), the single TMODE_MODE=2 VALSET is
            # silently coalesced and the receiver stays in survey-in
            # — same edge-triggered semantics documented in
            # ``configure_survey_in``.  The visible symptom is that
            # "Restore Past Survey" appears to succeed (200 OK, ACK
            # received) but ``NAV-SVIN.dur`` keeps ticking and
            # ``base-config.mode`` stays ``survey_in``.
            self._send_cfg_valset_locked(
                [("CFG_TMODE_MODE", 0)], layer=self._TMODE_DISABLE_ALL_LAYERS
            )
            time.sleep(self._TMODE_RESTART_DELAY_S)
            # Write to RAM+Flash (layer=5) directly via CFG-VALSET.
            # The legacy ``save_to_flash`` path (CFG-CFG saveMask) does
            # NOT reliably persist key/value-based TMODE config on
            # Gen9+ receivers (ZED-F9P) — observed symptom: after a
            # successful survey + auto-commit, a subsequent CFG-RST
            # hardware reset reverted to the *prior* flashed config
            # (e.g. an older ECEF Orig Survey) instead of the
            # just-committed LLH coordinates.  Writing directly to
            # layer=5 ensures Flash holds the new TMODE config
            # immediately, before any subsequent reset can wipe RAM.
            self._send_cfg_valset_locked(cfg_data, layer=5)  # RAM + Flash

            # Verify-and-retry: read back the ECEF triple and confirm it
            # matches what was just written — not merely non-zero.  A
            # stale non-zero ECEF left over from a *previous* fixed-base
            # config would pass a bare non-zero check while still being
            # wrong, so we compare against the values derived from this
            # call's LLH input.  A silent no-op here (ACK received but
            # ECEF unchanged) is exactly the failure mode that let a
            # "successfully configured" base emit zero RTCM frames.
            expected_ecef = (ecef_x_cm, ecef_y_cm, ecef_z_cm)
            ecef_x, ecef_y, ecef_z = self._read_ecef_locked()
            if (ecef_x, ecef_y, ecef_z) != expected_ecef:
                logger.warning(
                    "ECEF position mismatch after first write "
                    "(got %d,%d,%d cm, expected %d,%d,%d cm) — retrying",
                    ecef_x,
                    ecef_y,
                    ecef_z,
                    *expected_ecef,
                )
                self._send_cfg_valset_locked(cfg_data, layer=5)
                ecef_x, ecef_y, ecef_z = self._read_ecef_locked()
                if (ecef_x, ecef_y, ecef_z) != expected_ecef:
                    raise RuntimeError(
                        "Fixed base position did not take effect: "
                        f"receiver reports ECEF={ecef_x},{ecef_y},{ecef_z} cm, "
                        f"expected {expected_ecef} cm after two write "
                        "attempts. Try disconnecting and reconnecting, "
                        "or power-cycle the receiver."
                    )

            # Issue #63: no longer force-applies the RTCM-only output
            # profile / stationary dynamics here — see the matching
            # comment in ``configure_survey_in``.
        logger.info(
            "Fixed base configured: %.7f, %.7f, %.2fm (ECEF cm: %d, %d, %d)",
            config.latitude,
            config.longitude,
            config.altitude_m,
            ecef_x,
            ecef_y,
            ecef_z,
        )

    def _read_cfg_keys_locked(
        self, keys: list[str], layer: int = _CFG_LAYER_RAM
    ) -> dict[str, int]:
        """Poll arbitrary CFG keys at ``layer`` (must hold ``self._lock``).

        Used to verify a CFG-VALSET write actually took effect — same
        read-back pattern as ``_read_ecef_locked``, generalised to an
        arbitrary key list (issue #42). Defaults to the RAM layer,
        matching this method's behaviour before it accepted a layer
        parameter (issue #94).

        A key the receiver doesn't recognise — or that was never
        written at ``layer`` — is simply absent from the returned
        dict, distinguishable from a key that read back with an
        unexpected value. Callers that previously relied on a
        sentinel ``-1`` for "unknown key" must not: this method now
        reports that state as key-not-present instead (issue #94).

        Raises:
            RuntimeError: if the receiver NAKs the poll (naming the
                NAK), or if neither a CFG-VALGET nor an ACK-NAK
                response arrives within the read budget.
        """
        ser, reader = self._require_connection()

        poll_keys: list[str | int] = list(keys)
        msg = UBXMessage.config_poll(layer, 0, poll_keys)
        ser.reset_input_buffer()
        ser.write(msg.serialize())  # type: ignore[union-attr]

        for _ in range(_MAX_READ_ATTEMPTS):
            try:
                raw, parsed = reader.read()  # type: ignore[misc]
                if parsed is None:
                    continue
                identity = getattr(parsed, "identity", "")
                if identity == "CFG-VALGET":
                    result: dict[str, int] = {}
                    for key in keys:
                        val = getattr(parsed, key, None)
                        if val is not None:
                            result[key] = int(val)
                    return result
                if identity == "ACK-NAK":
                    raise RuntimeError(
                        f"Device rejected CFG-VALGET poll for {keys} (NAK)"
                    )
            except RuntimeError:
                raise
            except Exception:
                continue

        raise RuntimeError("No CFG-VALGET response for config keys")

    def _read_cfg_keys_with_retry_locked(
        self,
        keys: list[str],
        layer: int = _CFG_LAYER_RAM,
        attempts: int = 3,
    ) -> dict[str, int]:
        """Poll ``keys`` via ``_read_cfg_keys_locked``, retrying a bare timeout.

        Must hold ``self._lock``. A large poll (dozens of keys, e.g. the
        60-key RTCM port matrix) racing against RTCM/NAV traffic streamed
        on the same UART can miss its CFG-VALGET reply within one read
        budget even though the receiver is healthy and answers on the
        next attempt (issue #119) — each attempt re-drains and re-polls
        from scratch, so a miss here is a lost round trip, not a stuck
        receiver. An explicit ACK-NAK is a different failure shape (the
        receiver rejected the poll outright) and is never retried — it
        propagates immediately so it stays distinguishable from a
        transient timeout.
        """
        last_error = RuntimeError("No CFG-VALGET response for config keys")
        for attempt in range(1, attempts + 1):
            try:
                return self._read_cfg_keys_locked(keys, layer=layer)
            except RuntimeError as exc:
                if "NAK" in str(exc):
                    raise
                last_error = exc
                logger.warning(
                    "CFG-VALGET poll for %d keys got no response (attempt %d/%d)",
                    len(keys),
                    attempt,
                    attempts,
                )
        raise last_error

    def _write_and_verify_locked(
        self, cfg_data: list[tuple[str, int]], layer: int, label: str
    ) -> None:
        """Send a CFG-VALSET and verify the read-back, retrying once on mismatch.

        Must hold ``self._lock``. Shared verify-and-retry helper for the
        layer=5 writers touched by issue #42 — a bare ACK can lie about
        whether the value actually landed, so every durable write needs
        the same read-back check ``configure_fixed_base`` established for
        ECEF (issue #39).

        When ``layer`` includes the Flash bit, each attempt also polls
        the flash layer itself (issue #103) — the RAM check above only
        proves the receiver's *live* state is correct, not that it will
        survive a power cycle. A flash divergence never fails the write:
        RAM already matches, so the receiver is correctly configured
        right now. It folds into this method's existing single retry
        (a spurious first-attempt flash miss is re-written and re-polled
        the same as a RAM mismatch) and, if it survives that retry,
        queues an advisory on ``self._warnings`` naming the affected
        keys and the failure shape instead of raising.

        Raises:
            RuntimeError: if the RAM read-back still doesn't match after
                one retry.
        """
        expected = dict(cfg_data)
        check_flash = bool(layer & self._CFG_VALSET_FLASH_BIT)

        self._send_cfg_valset_locked(cfg_data, layer=layer)
        actual = self._read_cfg_keys_locked(list(expected))
        flash_divergence = (
            self._flash_divergence_locked(expected) if check_flash else None
        )
        if actual == expected and flash_divergence is None:
            return

        if actual != expected:
            logger.warning("%s mismatch after first write — retrying", label)
        self._send_cfg_valset_locked(cfg_data, layer=layer)
        actual = self._read_cfg_keys_locked(list(expected))
        if actual != expected:
            raise RuntimeError(
                f"{label} did not take effect: receiver reports {actual}, "
                f"expected {expected} after two write attempts. Try "
                "disconnecting and reconnecting, or power-cycle the "
                "receiver."
            )

        if check_flash:
            flash_divergence = self._flash_divergence_locked(expected)
            if flash_divergence is not None:
                message = f"{label}: {flash_divergence}"
                logger.warning(
                    "%s did not durably store the write after two attempts "
                    "— RAM is correctly configured, but this will not "
                    "survive a power cycle",
                    label,
                )
                self._warnings.append(message)

    def _flash_divergence_locked(self, expected: dict[str, int]) -> str | None:
        """Poll ``expected``'s keys at the flash layer and describe any divergence.

        Must hold ``self._lock``. Companion to ``_write_and_verify_locked``
        (issue #103) — never raises for a divergence, only for a transport
        failure: ``_read_cfg_keys_locked`` raises ``RuntimeError`` both for
        an explicit ACK-NAK (one of the three divergence shapes this method
        reports, caught below) and for no response at all within the read
        budget (a genuine communication failure, not one of the shapes the
        spec describes — left to propagate rather than softened into a
        warning). Returns ``None`` when flash agrees with what was just
        written to RAM, else a message naming the affected keys and which
        of the three observed shapes occurred: a value differs from what
        was written, the key comes back NAK'd, or it's silently omitted
        from a partial batch response.
        """
        try:
            flash_actual = self._read_cfg_keys_locked(
                list(expected), layer=self._CFG_LAYER_FLASH
            )
        except RuntimeError as exc:
            if "NAK" not in str(exc):
                raise
            return f"flash NAK'd the read-back for {list(expected)}: {exc}"

        omitted = sorted(k for k in expected if k not in flash_actual)
        differing = {
            k: v for k, v in sorted(flash_actual.items()) if expected.get(k) != v
        }
        if not omitted and not differing:
            return None

        parts: list[str] = []
        if differing:
            mismatches = ", ".join(
                f"{k}={v} (expected {expected[k]})" for k, v in differing.items()
            )
            parts.append(f"value differs at flash: {mismatches}")
        if omitted:
            parts.append(f"omitted from flash read-back: {', '.join(omitted)}")
        return "; ".join(parts)

    # ------------------------------------------------------------------
    # Multi-port RTCM configuration
    # ------------------------------------------------------------------

    def get_rtcm_port_config(self) -> RtcmPortConfig:
        """Read RTCM output config for ALL ports from the receiver.

        Polls ``CFG_MSGOUT_RTCM_3X_TYPE*_{USB,UART1,UART2,I2C,SPI}``
        and returns a matrix of row id → {port: rate}.
        """
        with self._lock:
            return self._get_rtcm_port_config_locked()

    def _get_rtcm_port_config_locked(self) -> RtcmPortConfig:
        """Read multi-port RTCM config (must hold self._lock)."""
        # Build key list: 12 rows × 5 ports = 60 keys
        all_keys = [
            _rtcm_key(msg_id, port) for msg_id in _ALL_RTCM_IDS for port in _RTCM_PORTS
        ]
        values = self._read_cfg_keys_with_retry_locked(all_keys)
        return self._parse_rtcm_port_valget(values)

    @staticmethod
    def _parse_rtcm_port_valget(values: dict[str, int]) -> RtcmPortConfig:
        """Build a multi-port RTCM config from polled CFG key values."""
        messages: dict[RtcmRowId, dict[str, int]] = {}

        for msg_id in _ALL_RTCM_IDS:
            port_rates: dict[str, int] = {}
            for port in _RTCM_PORTS:
                key = _rtcm_key(msg_id, port)
                port_rates[port] = values.get(key, 0)
            messages[msg_id] = port_rates

        return RtcmPortConfig(messages=messages)

    def configure_rtcm_ports(self, config: RtcmPortConfig) -> None:
        """Apply multi-port RTCM output configuration to the receiver.

        Sends a CFG-VALSET with rates for each message on each port,
        writes to RAM+Flash (layer=5), and verifies the read-back — a
        RAM-only write reverted to the last-flashed message selection
        on reboot / reconnect (issue #42).
        """
        cfg_data: list[tuple[str, int]] = []

        for msg_id, port_rates in config.messages.items():
            for port, rate in port_rates.items():
                key = _rtcm_key(msg_id, port)
                cfg_data.append((key, rate))

        if not cfg_data:
            logger.warning("No valid RTCM port config to apply")
            return

        with self._lock:
            self._write_and_verify_locked(cfg_data, layer=5, label="RTCM port config")
        logger.info("RTCM multi-port config applied (%d keys)", len(cfg_data))

    # ------------------------------------------------------------------
    # Port protocol configuration
    # ------------------------------------------------------------------

    def get_port_protocols(self) -> PortProtocolConfig:
        """Read live in/out protocol state for UART1, UART2 and USB.

        Polls all ``CFG_{PORT}{IN,OUT}PROT_{UBX,NMEA,RTCM3X}`` keys —
        the driver previously only polled the six UART OUTPROT keys;
        this covers INPROT and USB too (issue #57).
        """
        with self._lock:
            return self._get_port_protocols_locked()

    def _get_port_protocols_locked(self) -> PortProtocolConfig:
        """Read port protocol config for all ports (must hold self._lock)."""
        all_keys = [
            _protocol_key(port, direction, protocol)
            for port in PortId
            for direction in _PROTOCOL_DIRECTIONS
            for protocol in UbxProtocol
        ]
        values = self._read_cfg_keys_with_retry_locked(all_keys)
        return self._parse_port_protocols_valget(values)

    @staticmethod
    def _parse_port_protocols_valget(values: dict[str, int]) -> PortProtocolConfig:
        """Build a port protocol config from polled CFG key values."""
        in_protocols: dict[PortId, list[UbxProtocol]] = {}
        out_protocols: dict[PortId, list[UbxProtocol]] = {}

        for port in PortId:
            in_protocols[port] = [
                protocol
                for protocol in UbxProtocol
                if values.get(_protocol_key(port, "IN", protocol), 0) == 1
            ]
            out_protocols[port] = [
                protocol
                for protocol in UbxProtocol
                if values.get(_protocol_key(port, "OUT", protocol), 0) == 1
            ]

        return PortProtocolConfig(
            in_protocols=in_protocols, out_protocols=out_protocols
        )

    # ------------------------------------------------------------------
    # GNSS constellation configuration
    # ------------------------------------------------------------------

    # u-blox gnssId → GnssConstellation mapping
    _GNSS_ID_MAP: dict[int, GnssConstellation] = {
        0: GnssConstellation.GPS,
        1: GnssConstellation.SBAS,
        2: GnssConstellation.GALILEO,
        3: GnssConstellation.BEIDOU,
        5: GnssConstellation.QZSS,
        6: GnssConstellation.GLONASS,
    }

    def get_gnss_config(self) -> GnssConfig:
        """Poll CFG-GNSS and return current constellation configuration."""
        with self._lock:
            return self._get_gnss_config_locked()

    def _get_gnss_config_locked(self) -> GnssConfig:
        """Read GNSS config (must hold self._lock)."""
        ser, reader = self._require_connection()

        poll_msg = UBXMessage("CFG", "CFG-GNSS", POLL)
        ser.reset_input_buffer()
        ser.write(poll_msg.serialize())

        for _ in range(_MAX_READ_ATTEMPTS):
            try:
                raw, parsed = reader.read()  # type: ignore[misc]
                if parsed is not None and hasattr(parsed, "identity"):
                    if parsed.identity == "CFG-GNSS":
                        return self._parse_cfg_gnss(parsed)
            except Exception:
                continue

        raise RuntimeError("No CFG-GNSS response from device")

    @classmethod
    def _parse_cfg_gnss(cls, parsed: object) -> GnssConfig:
        """Parse a CFG-GNSS response into a GnssConfig model."""
        num_config_blocks = int(getattr(parsed, "numConfigBlocks", 0))
        systems: list[GnssSystemConfig] = []

        for i in range(num_config_blocks):
            # pyubx2 always uses 1-indexed suffixes: _01, _02, ...
            suffix = f"_{i + 1:02d}"
            gnss_id = int(getattr(parsed, f"gnssId{suffix}", 255))
            enabled_raw = int(getattr(parsed, f"enable{suffix}", 0))
            # Fallback: check flags field bit 0
            if not hasattr(parsed, f"enable{suffix}"):
                flags = int(getattr(parsed, f"flags{suffix}", 0))
                enabled_raw = flags & 0x01
            min_ch = int(getattr(parsed, f"resTrkCh{suffix}", 0))
            max_ch = int(getattr(parsed, f"maxTrkCh{suffix}", 0))
            sig_mask = int(getattr(parsed, f"sigCfMask{suffix}", 0))

            constellation = cls._GNSS_ID_MAP.get(gnss_id)
            if constellation is not None:
                systems.append(
                    GnssSystemConfig(
                        constellation=constellation,
                        enabled=bool(enabled_raw),
                        min_channels=min_ch,
                        max_channels=max_ch,
                        sig_cfg_mask=sig_mask,
                    )
                )

        return GnssConfig(systems=systems)

    def configure_gnss(self, constellations: set[GnssConstellation]) -> None:
        """Assertive durable write of the six per-constellation enable keys (issue #104).

        Replaces the legacy CFG-GNSS SET block write, which has no
        layer concept and was therefore RAM-only on every unit — a
        constellation change made through it never survived a power
        cycle. Every key in :data:`_GNSS_SIGNAL_ENA_KEYS` is written
        explicitly (on for a wanted constellation, off otherwise) at
        layer=5 (RAM+Flash) via ``_write_and_verify_locked``, which
        gives this write the same RAM read-back retry and flash
        read-back divergence advisory as every other durable writer —
        including the flash divergence case this method's docstring in
        ``_write_and_verify_locked`` describes. Channel allocation is
        untouched: this call has no channel parameter, by design —
        allocation is left to the firmware.

        Immediately after the write lands, the legacy CFG-GNSS block
        is polled again and compared against ``constellations``. A
        disagreement — the write ACKed and reads back correctly at
        both RAM and Flash, but the firmware's own block still
        disagrees — is queued on ``self._warnings`` rather than
        raised: this can never show up as a field difference, since
        ``ReceiverAssertion.constellations`` and this write both read
        the same six keys and would simply report a match. See
        ``GpsReceiverDriver.drain_warnings`` for the channel this
        lands on.
        """
        cfg_data = [
            (key, int(constellation in constellations))
            for constellation, key in _GNSS_SIGNAL_ENA_KEYS.items()
        ]
        with self._lock:
            self._write_and_verify_locked(
                cfg_data, layer=5, label="GNSS constellations"
            )
            disagreement = self._gnss_probe_disagreement_locked(constellations)
            if disagreement is not None:
                self._warnings.append(f"GNSS constellations: {disagreement}")

        logger.info(
            "GNSS constellations configured: %s",
            sorted(c.value for c in constellations),
        )

    def _gnss_probe_disagreement_locked(
        self, expected: set[GnssConstellation]
    ) -> str | None:
        """Poll the legacy CFG-GNSS block and describe any disagreement with ``expected``.

        Must hold ``self._lock``. The block SET is deprecated in
        favour of the six ``CFG_SIGNAL_*_ENA`` keys (issue #104), but
        the block POLL survives as a capability probe — the only way
        to see whether the firmware actually acts on an enable key it
        ACKed. Returns ``None`` when the block agrees with
        ``expected``.
        """
        probed = set(self._get_gnss_config_locked().enabled_constellations())
        if probed == expected:
            return None
        return (
            f"legacy CFG-GNSS block reports {sorted(c.value for c in probed)}, "
            f"enable keys report {sorted(c.value for c in expected)} — the "
            "write landed but this firmware may not act on it"
        )

    # ------------------------------------------------------------------
    # Apply-config primitives (issue #61)
    # ------------------------------------------------------------------

    def configure_port_protocols(
        self,
        in_protocols: dict[PortId, list[UbxProtocol]],
        out_protocols: dict[PortId, list[UbxProtocol]],
    ) -> None:
        """Assertive per-port protocol write for exactly the ports given.

        Only ports present in either mapping are touched. For a
        touched port+direction, every one of the three protocol keys
        is written explicitly (on and off) — this is what makes the
        write assertive rather than additive.
        """
        cfg_data: list[tuple[str, int]] = []
        for port in set(in_protocols) | set(out_protocols):
            if port in in_protocols:
                wanted_in = set(in_protocols[port])
                for protocol in UbxProtocol:
                    cfg_data.append(
                        (
                            _protocol_key(port, "IN", protocol),
                            int(protocol in wanted_in),
                        )
                    )
            if port in out_protocols:
                wanted_out = set(out_protocols[port])
                for protocol in UbxProtocol:
                    cfg_data.append(
                        (
                            _protocol_key(port, "OUT", protocol),
                            int(protocol in wanted_out),
                        )
                    )

        if not cfg_data:
            return
        with self._lock:
            self._write_and_verify_locked(
                cfg_data, layer=5, label="Port protocol config"
            )

    def configure_measurement_rate(self, period_ms: int) -> None:
        """Write ``CFG_RATE_MEAS=period_ms`` and pin ``CFG_RATE_NAV=1``.

        ``period_ms`` is already the raw measurement period in
        milliseconds — no Hz conversion happens here or anywhere else
        on this path (the UI does that conversion for display only).
        """
        cfg_data = [("CFG_RATE_MEAS", period_ms), ("CFG_RATE_NAV", 1)]
        with self._lock:
            self._write_and_verify_locked(cfg_data, layer=5, label="Measurement rate")

    def configure_dyn_model(self, model: DynModel) -> None:
        """Write ``CFG_NAVSPG_DYNMODEL`` for an arbitrary dynamics class."""
        with self._lock:
            self._write_and_verify_locked(
                [("CFG_NAVSPG_DYNMODEL", _DYN_MODEL_VALUES[model])],
                layer=5,
                label="Dynamics model",
            )

    def get_dyn_model(self) -> DynModel:
        """Read the receiver's current ``CFG_NAVSPG_DYNMODEL``."""
        with self._lock:
            actual = self._read_cfg_keys_locked(["CFG_NAVSPG_DYNMODEL"])
        return _DYN_MODEL_NAMES[actual["CFG_NAVSPG_DYNMODEL"]]

    def configure_tmode_mode(self, mode: BaseMode) -> None:
        """Write ``CFG_TMODE_MODE`` directly, without touching position keys.

        A plain mode assertion for apply-config's role-fields step —
        NOT a full base-mode transition. Coordinate preconditions for
        ``fixed`` mode are the caller's responsibility.
        """
        with self._lock:
            self._write_and_verify_locked(
                [("CFG_TMODE_MODE", _TMODE_MODE_VALUES[mode])],
                layer=5,
                label="TMODE mode",
            )

    def configure_optimisations(
        self,
        elevation_mask_deg: int | None,
        bds_b2_enabled: bool | None,
        spi_enabled: bool | None,
    ) -> None:
        """Write only the optimisation fields provided.

        Each field is independently optional — ``None`` means leave
        the receiver's current value untouched. Unlike ``ports`` and
        ``apply_rtcm_matrix``, this write is NOT assertive.
        """
        cfg_data: list[tuple[str, int]] = []
        if elevation_mask_deg is not None:
            cfg_data.append(("CFG_NAVSPG_INFIL_MINELEV", elevation_mask_deg))
        if bds_b2_enabled is not None:
            cfg_data.append(("CFG_SIGNAL_BDS_B2_ENA", int(bds_b2_enabled)))
        if spi_enabled is not None:
            cfg_data.append(("CFG_SPI_ENABLED", int(spi_enabled)))

        if not cfg_data:
            return
        with self._lock:
            self._write_and_verify_locked(
                cfg_data, layer=5, label="Optimisation settings"
            )

    def apply_rtcm_matrix(self, matrix: dict[RtcmRowId, dict[PortId, bool]]) -> None:
        """Assertive write of all 36 cells (12 rows x UART1/UART2/USB).

        Deliberately a single write with no internal retry-and-raise,
        unlike this driver's other layer=5 writers: the apply-config
        endpoint owns the read-back verify for this specific write as
        a first-class part of its contract (a per-cell diff returned
        to the caller with a 200, nothing rolled back) rather than
        raising here.
        """
        cfg_data = [
            (_rtcm_key(row, port.value), int(matrix.get(row, {}).get(port, False)))
            for row in _ALL_RTCM_IDS
            for port in _MATRIX_PORTS
        ]
        with self._lock:
            self._send_cfg_valset_locked(cfg_data, layer=5)

    def get_uart_baud_rates(self) -> dict[PortId, int]:
        """Read the live UART1/UART2 baud rates.

        Used only for apply-config's non-blocking throughput estimate.
        """
        with self._lock:
            raw = self._read_cfg_keys_locked(
                ["CFG_UART1_BAUDRATE", "CFG_UART2_BAUDRATE"]
            )
        return {
            PortId.UART1: raw["CFG_UART1_BAUDRATE"],
            PortId.UART2: raw["CFG_UART2_BAUDRATE"],
        }

    def configure_baud(self, uart1: int | None, uart2: int | None) -> None:
        """Write only the UART baud fields provided (issue #62).

        Deliberately the last apply-config write — see the module's
        write-ordering rationale in ``DeviceService.apply_receiver_config``.
        """
        cfg_data: list[tuple[str, int]] = []
        if uart1 is not None:
            cfg_data.append(("CFG_UART1_BAUDRATE", uart1))
        if uart2 is not None:
            cfg_data.append(("CFG_UART2_BAUDRATE", uart2))

        if not cfg_data:
            return
        with self._lock:
            self._send_cfg_valset_locked(cfg_data, layer=5)

    def drain_warnings(self) -> list[str]:
        """Drain flash-divergence advisories queued by durable writes (issue #103).

        The only producer on this driver is ``_write_and_verify_locked``'s
        flash read-back — see its docstring for what lands here.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to device")
        with self._lock:
            drained, self._warnings = self._warnings, []
        return drained

    def get_receiver_scalars(self) -> ReceiverScalarConfig:
        """Batched read of baud, meas rate, dyn model, tmode mode, the
        enabled constellations and the three optimisation fields — one
        CFG-VALGET poll (issue #97, extended by issue #104).

        Replaces what would otherwise be up to seven separate getters:
        the three that already existed (``get_dyn_model``,
        ``get_uart_baud_rates``, the TMODE portion of ``get_base_config``)
        plus the four that had no standalone getter at all
        (measurement rate, elevation mask, BeiDou B2, SPI). The six
        ``CFG_SIGNAL_*_ENA`` constellation-enable keys (issue #104) fold
        into this same poll for free — ``ReceiverAssertion.constellations``
        no longer needs a separate CFG-GNSS block read to populate.
        """
        with self._lock:
            raw = self._read_cfg_keys_locked(_RECEIVER_SCALAR_KEYS)
        constellations = [
            constellation
            for constellation, key in _GNSS_SIGNAL_ENA_KEYS.items()
            if raw[key]
        ]
        return ReceiverScalarConfig(
            uart1_baud=raw["CFG_UART1_BAUDRATE"],
            uart2_baud=raw["CFG_UART2_BAUDRATE"],
            meas_period_ms=raw["CFG_RATE_MEAS"],
            dyn_model=_DYN_MODEL_NAMES[raw["CFG_NAVSPG_DYNMODEL"]],
            tmode_mode=_TMODE_MODE_NAMES[raw["CFG_TMODE_MODE"]],
            constellations=constellations,
            elevation_mask_deg=raw["CFG_NAVSPG_INFIL_MINELEV"],
            bds_b2_enabled=bool(raw["CFG_SIGNAL_BDS_B2_ENA"]),
            spi_enabled=bool(raw["CFG_SPI_ENABLED"]),
        )

    def reconnect_at_baud(self, baud_rate: int) -> DeviceInfo:
        """Reopen the serial port at ``baud_rate`` without resetting the receiver.

        Unlike ``reset_and_reconnect``, this doesn't touch the chip.
        A baud write's ACK goes out over the wire at the *old* baud,
        just before the UART peripheral itself switches — by the time
        ``configure_baud`` returns, the receiver side is already done.
        Only the host's own pyserial handle needs to reopen at the
        new rate and redo the MON-VER handshake to confirm the link
        is live (issue #62).

        Raises:
            RuntimeError: If the driver was never connected (no saved
                port to reopen).
        """
        if self._port is None:
            raise RuntimeError(
                "Cannot reopen at a new baud — driver was never connected "
                "(no saved port to reopen)."
            )
        port = self._port
        with self._lock:
            self._cleanup()
        return self.connect(port, baud_rate)

    def save_to_flash(self) -> None:
        """Save current RAM config to BBR + Flash (layers 7)."""
        # CFG-CFG: save current config to all non-volatile layers
        msg = UBXMessage(
            "CFG",
            "CFG-CFG",
            SET,
            saveMask=b"\x1f\x1f\x00\x00",  # Save all sections
            deviceMask=b"\x17",  # BBR + Flash + SPI flash
        )
        with self._lock:
            ser, _ = self._require_connection()
            ser.reset_input_buffer()
            ser.write(msg.serialize())
            self._wait_for_ack("CFG-CFG")
        logger.info("Configuration saved to flash")

    # ------------------------------------------------------------------
    # Status polling
    # ------------------------------------------------------------------

    # NAV-PVT fixType mapping (u-blox → vendor-neutral)
    _FIX_TYPE_MAP: dict[int, GpsFixType] = {
        0: GpsFixType.NO_FIX,
        1: GpsFixType.DEAD_RECKONING,
        2: GpsFixType.FIX_2D,
        3: GpsFixType.FIX_3D,
        4: GpsFixType.GNSS_DR,
        5: GpsFixType.TIME_ONLY,
    }

    def get_position(self) -> GpsPosition:
        """Poll NAV-PVT and return a vendor-neutral position snapshot."""
        with self._lock:
            ser, reader = self._require_connection()

            # Poll NAV-PVT
            poll_msg = UBXMessage("NAV", "NAV-PVT", POLL)
            ser.reset_input_buffer()
            ser.write(poll_msg.serialize())

            # Read response
            for _ in range(_MAX_READ_ATTEMPTS):
                try:
                    raw, parsed = reader.read()  # type: ignore[misc]
                    if parsed is not None and hasattr(parsed, "identity"):
                        if parsed.identity == "NAV-PVT":
                            return self._parse_nav_pvt(parsed)
                except Exception:
                    continue

            return GpsPosition()  # Default if no response

    def _parse_nav_pvt(self, parsed: object) -> GpsPosition:
        """Parse a NAV-PVT message into a GpsPosition model."""
        from datetime import datetime, timezone

        # Fix type
        fix_type_raw = int(getattr(parsed, "fixType", 0))
        fix_type = self._FIX_TYPE_MAP.get(fix_type_raw, GpsFixType.NO_FIX)

        # RTK status from carrier solution flags
        # carrSoln: 0=none, 1=float, 2=fixed
        carr_soln = int(getattr(parsed, "carrSoln", 0))
        rtk_map = {0: "none", 1: "float", 2: "fixed"}
        rtk_status = rtk_map.get(carr_soln, "none")

        # NAV-PVT scaling: pyubx2 >=1.3.0 pre-scales fields whose
        # payload spec declares a scale factor (lat/lon: 1e-7,
        # pDOP: 0.01, headMot/headAcc: 1e-5).  Applying those factors
        # again here would double-scale (e.g. lat ≈ 3.27e-6° instead
        # of 32.7°).  The mm-valued integer fields below have no
        # spec scale factor and still need the /1000.0 to convert to
        # metres.  See pyubx2.UBX_PAYLOADS_GET['NAV-PVT'].
        lat = float(getattr(parsed, "lat", 0.0))
        lon = float(getattr(parsed, "lon", 0.0))
        # Height above ellipsoid in mm → m
        h_ell = float(getattr(parsed, "height", 0)) / 1000.0
        # Height above MSL in mm → m
        h_msl = float(getattr(parsed, "hMSL", 0)) / 1000.0

        # Accuracy estimates in mm → m
        h_acc = float(getattr(parsed, "hAcc", 0)) / 1000.0
        v_acc = float(getattr(parsed, "vAcc", 0)) / 1000.0

        # Satellites
        num_sv = int(getattr(parsed, "numSV", 0))

        # Speed in mm/s → m/s
        g_speed = float(getattr(parsed, "gSpeed", 0)) / 1000.0

        # Heading (pre-scaled to degrees by pyubx2)
        head_mot = float(getattr(parsed, "headMot", 0.0))

        # pDOP (pre-scaled by pyubx2; 99.9 sentinel when missing)
        pdop = float(getattr(parsed, "pDOP", 99.9))

        # Timestamp from NAV-PVT fields
        ts: datetime | None = None
        try:
            year = int(getattr(parsed, "year", 0))
            month = int(getattr(parsed, "month", 0))
            day = int(getattr(parsed, "day", 0))
            hour = int(getattr(parsed, "hour", 0))
            minute = int(getattr(parsed, "min", 0))
            second = int(getattr(parsed, "second", 0))
            nano = int(getattr(parsed, "nano", 0))
            if year >= 2000:
                micro = max(0, nano // 1000)
                ts = datetime(
                    year, month, day, hour, minute, second, micro, tzinfo=timezone.utc
                )
        except (ValueError, OverflowError):
            pass

        return GpsPosition(
            fix_type=fix_type,
            rtk_status=rtk_status,
            latitude=lat,
            longitude=lon,
            altitude_m=h_ell,
            altitude_msl_m=h_msl,
            horizontal_accuracy_m=h_acc,
            vertical_accuracy_m=v_acc,
            num_satellites=num_sv,
            speed_m_s=max(0.0, g_speed),
            heading_deg=head_mot,
            pdop=pdop,
            timestamp=ts,
        )

    def get_survey_in_status(self) -> SurveyInProgress:
        with self._lock:
            return self._get_survey_in_locked()

    def send_cfg_rst_diagnostic(
        self,
        reset_mode: int,
        wait_seconds: float,
        bbr_bits: dict[str, int],
        read_after_state: bool = True,
    ) -> tuple[SurveyInProgress, SurveyInProgress | None, bytes]:
        """Send an arbitrary UBX-CFG-RST and capture before/after state.

        Diagnostic-only entry point — exposed by the
        ``POST /api/device/debug/cfg-rst`` endpoint so the canonical
        reset variant for clearing the HPG 1.12 NAV-SVIN accumulator
        can be discovered empirically.  Holds ``self._lock`` for the
        full before-write-wait-after cycle so a concurrent NAV poll
        cannot interleave and corrupt the timing.

        Args:
            reset_mode: UBX ``resetMode`` byte.  Validate at the
                caller — this method does not gate values.
            wait_seconds: How long to sleep after the write before
                reading the after-state.  Ignored when
                ``read_after_state=False``.
            bbr_bits: Named BBR-clear bits, e.g. ``{"pos": 1,
                "eph": 0}``.  Passed straight to the ``UBXMessage``
                constructor.  Unknown keys raise.
            read_after_state: When ``False``, skip the post-write
                sleep AND the NAV-SVIN after-poll.  Required for
                ``resetMode=0`` / ``resetMode=4`` (hardware resets)
                because the receiver re-enumerates on the USB bus
                during the reset and the after-poll would hang on
                a disconnected serial port.

        Returns:
            Tuple of ``(before, after, ubx_bytes_sent)``.  When
            ``read_after_state=False`` ``after`` is ``None``;
            otherwise both are ``SurveyInProgress`` reads of
            NAV-SVIN.  ``ubx_bytes_sent`` is the serialised UBX frame
            for hex display in the diagnostic UI.
        """
        with self._lock:
            before = self._get_survey_in_locked()
            ser, _ = self._require_connection()
            # pyubx2's UBXMessage __init__ declares ``parsebitfield``
            # as Literal[0,1,2] — passing ``**bbr_bits: dict[str, int]``
            # widens the kwargs type and trips strict mode.  The
            # endpoint validates ``bbr_bits`` against a known
            # allowlist before reaching this method, so the widened
            # type is safe in practice.
            msg = UBXMessage(
                "CFG",
                "CFG-RST",
                SET,
                resetMode=reset_mode,
                **bbr_bits,  # type: ignore[arg-type]
            )
            wire_bytes: bytes = msg.serialize()  # type: ignore[union-attr]
            ser.reset_input_buffer()
            ser.write(wire_bytes)
            after: SurveyInProgress | None = None
            if read_after_state:
                time.sleep(wait_seconds)
                after = self._get_survey_in_locked()
        if after is not None:
            logger.info(
                "Diagnostic CFG-RST sent: resetMode=0x%02x bits=%s; "
                "dur %d -> %d, obs %d -> %d",
                reset_mode,
                bbr_bits,
                before.duration_seconds,
                after.duration_seconds,
                before.observations,
                after.observations,
            )
        else:
            logger.info(
                "Diagnostic CFG-RST sent (fire-and-forget): "
                "resetMode=0x%02x bits=%s; before dur=%d obs=%d",
                reset_mode,
                bbr_bits,
                before.duration_seconds,
                before.observations,
            )
        return before, after, wire_bytes

    def _get_survey_in_locked(self) -> SurveyInProgress:
        """Poll NAV-SVIN (must hold self._lock)."""
        ser, reader = self._require_connection()

        poll_msg = UBXMessage("NAV", "NAV-SVIN", POLL)
        ser.reset_input_buffer()
        ser.write(poll_msg.serialize())

        for _ in range(_MAX_READ_ATTEMPTS):
            try:
                raw, parsed = reader.read()  # type: ignore[misc]
                if parsed is not None and hasattr(parsed, "identity"):
                    if parsed.identity == "NAV-SVIN":
                        is_valid = bool(getattr(parsed, "valid", 0))

                        # Extract position when valid
                        lat: float | None = None
                        lon: float | None = None
                        alt: float | None = None

                        if is_valid:
                            lat, lon, alt = self.extract_svin_position(parsed)

                        return SurveyInProgress(
                            active=bool(getattr(parsed, "active", 0)),
                            valid=is_valid,
                            duration_seconds=int(getattr(parsed, "dur", 0)),
                            mean_accuracy_mm=float(getattr(parsed, "meanAcc", 0))
                            / 10.0,
                            observations=int(getattr(parsed, "obs", 0)),
                            latitude=lat,
                            longitude=lon,
                            altitude_m=alt,
                        )
            except Exception:
                continue

        return SurveyInProgress()  # Default if no response

    @staticmethod
    def _ecef_to_llh(x_m: float, y_m: float, z_m: float) -> tuple[float, float, float]:
        """Convert ECEF coordinates (metres) to WGS84 lat/lon/alt.

        Uses an iterative method for sub-mm accuracy.

        Returns:
            Tuple of (latitude_deg, longitude_deg, altitude_m).
        """
        import math

        a = 6378137.0  # WGS84 semi-major axis
        f = 1.0 / 298.257223563  # WGS84 flattening
        e2 = 2.0 * f - f * f  # eccentricity squared

        lon = math.atan2(y_m, x_m)
        p = math.sqrt(x_m * x_m + y_m * y_m)

        # Initial latitude estimate
        lat = math.atan2(z_m, p * (1.0 - e2))

        # Iterate for convergence
        for _ in range(10):
            sin_lat = math.sin(lat)
            n = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
            lat = math.atan2(z_m + e2 * n * sin_lat, p)

        sin_lat = math.sin(lat)
        n = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
        alt = p / math.cos(lat) - n

        return (math.degrees(lat), math.degrees(lon), alt)

    @staticmethod
    def _llh_to_ecef(
        lat_deg: float, lon_deg: float, alt_m: float
    ) -> tuple[float, float, float]:
        """Convert WGS84 lat/lon/alt to ECEF coordinates (metres).

        Inverse of ``_ecef_to_llh``. Used to populate
        ``CFG_TMODE_ECEF_X/Y/Z`` alongside the LLH keys when writing a
        fixed-base position — the ZED-F9P base engine requires a valid
        ECEF position before it will generate RTCM corrections.

        Returns:
            Tuple of (x_m, y_m, z_m).
        """
        import math

        a = 6378137.0  # WGS84 semi-major axis
        f = 1.0 / 298.257223563  # WGS84 flattening
        e2 = 2.0 * f - f * f  # eccentricity squared

        lat = math.radians(lat_deg)
        lon = math.radians(lon_deg)
        sin_lat = math.sin(lat)
        n = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)

        x = (n + alt_m) * math.cos(lat) * math.cos(lon)
        y = (n + alt_m) * math.cos(lat) * math.sin(lon)
        z = (n * (1.0 - e2) + alt_m) * sin_lat

        return (x, y, z)

    @staticmethod
    def _m_to_cm_hp(value_m: float) -> tuple[int, int]:
        """Split a metre value into wire-format (cm, HP) for CFG_TMODE_ECEF_*.

        Inverse of the ``cm / 100.0 + hp * 0.0001`` reconstruction used
        elsewhere in this file. HP (0.1 mm resolution, range -99..99)
        must share ``cm``'s sign per the u-blox interface spec, so the
        split truncates toward zero rather than using floor division
        (which would give HP the wrong sign for negative coordinates).
        """
        total_tenths_mm = round(value_m * 10000)
        cm, hp = divmod(abs(total_tenths_mm), 100)
        if total_tenths_mm < 0:
            cm, hp = -cm, -hp
        return cm, hp

    def _read_ecef_locked(self) -> tuple[int, int, int]:
        """Poll CFG_TMODE_ECEF_X/Y/Z (cm) from the receiver.

        Must hold ``self._lock``. Used to verify a fixed-base ECEF
        write actually took effect.
        """
        ser, reader = self._require_connection()

        keys: list[str | int] = [
            "CFG_TMODE_ECEF_X",
            "CFG_TMODE_ECEF_Y",
            "CFG_TMODE_ECEF_Z",
        ]
        msg = UBXMessage.config_poll(0, 0, keys)
        ser.reset_input_buffer()
        ser.write(msg.serialize())  # type: ignore[union-attr]

        for _ in range(_MAX_READ_ATTEMPTS):
            try:
                raw, parsed = reader.read()  # type: ignore[misc]
                if parsed is None:
                    continue
                identity = getattr(parsed, "identity", "")
                if identity == "CFG-VALGET":
                    return (
                        int(getattr(parsed, "CFG_TMODE_ECEF_X", 0)),
                        int(getattr(parsed, "CFG_TMODE_ECEF_Y", 0)),
                        int(getattr(parsed, "CFG_TMODE_ECEF_Z", 0)),
                    )
            except Exception:
                continue

        raise RuntimeError("No CFG-VALGET response for ECEF position")

    @staticmethod
    def extract_svin_position(parsed: object) -> tuple[float, float, float]:
        """Extract LLH position from a NAV-SVIN message.

        NAV-SVIN provides ECEF coordinates in cm + high-precision
        in 0.1mm. We convert to WGS84 lat/lon/alt.

        Args:
            parsed: Parsed NAV-SVIN UBX message.

        Returns:
            Tuple of (latitude_deg, longitude_deg, altitude_m).
        """
        # ECEF in cm + high-precision in 0.1mm
        mean_x_cm = float(getattr(parsed, "meanX", 0))
        mean_y_cm = float(getattr(parsed, "meanY", 0))
        mean_z_cm = float(getattr(parsed, "meanZ", 0))
        mean_x_hp = float(getattr(parsed, "meanXHP", 0))
        mean_y_hp = float(getattr(parsed, "meanYHP", 0))
        mean_z_hp = float(getattr(parsed, "meanZHP", 0))

        # Combine: cm → m, then add HP (0.1mm = 0.0001m)
        x_m = mean_x_cm / 100.0 + mean_x_hp * 0.0001
        y_m = mean_y_cm / 100.0 + mean_y_hp * 0.0001
        z_m = mean_z_cm / 100.0 + mean_z_hp * 0.0001

        return UbloxDriver._ecef_to_llh(x_m, y_m, z_m)

    def get_device_info(self) -> DeviceInfo:
        return self._poll_mon_ver()

    def get_base_config(self) -> CurrentBaseConfig:
        """Read current base station config via CFG-VALGET.

        Reads CFG_TMODE_MODE, CFG_TMODE_LAT, CFG_TMODE_LON,
        CFG_TMODE_HEIGHT, CFG_TMODE_FIXED_POS_ACC from the receiver.
        """
        with self._lock:
            return self._get_base_config_locked()

    def _get_base_config_locked(self) -> CurrentBaseConfig:
        """Read base config (must hold self._lock)."""
        ser, reader = self._require_connection()

        # Poll configuration values (layer 0 = RAM)
        # Request both LLH and ECEF fields — the receiver populates
        # whichever set matches POS_TYPE.
        keys = [
            "CFG_TMODE_MODE",
            "CFG_TMODE_POS_TYPE",
            "CFG_TMODE_LAT",
            "CFG_TMODE_LON",
            "CFG_TMODE_HEIGHT",
            "CFG_TMODE_ECEF_X",
            "CFG_TMODE_ECEF_Y",
            "CFG_TMODE_ECEF_Z",
            "CFG_TMODE_ECEF_X_HP",
            "CFG_TMODE_ECEF_Y_HP",
            "CFG_TMODE_ECEF_Z_HP",
            "CFG_TMODE_FIXED_POS_ACC",
        ]
        keys_any: list[str | int] = list(keys)
        msg = UBXMessage.config_poll(0, 0, keys_any)
        ser.reset_input_buffer()
        ser.write(msg.serialize())  # type: ignore[union-attr]

        # Read response — may take a few reads
        for i in range(_MAX_READ_ATTEMPTS):
            try:
                raw, parsed = reader.read()  # type: ignore[misc]
                if parsed is None:
                    logger.debug("get_base_config read %d: None", i)
                    continue
                identity = getattr(parsed, "identity", "")
                logger.debug("get_base_config read %d: %s", i, identity)
                if identity == "CFG-VALGET":
                    return self._parse_cfg_tmode(parsed)
            except Exception as exc:
                logger.debug("get_base_config read %d: exception %s", i, exc)
                continue

        raise RuntimeError("No CFG-VALGET response for TMODE config")

    @staticmethod
    def _parse_cfg_tmode(parsed: object) -> CurrentBaseConfig:
        """Parse CFG-VALGET TMODE response into CurrentBaseConfig.

        Handles both position storage formats:
        - POS_TYPE=0 (ECEF): reads ECEF_X/Y/Z + HP, converts to LLH
        - POS_TYPE=1 (LLH): reads LAT/LON/HEIGHT directly
        """
        mode_raw = int(getattr(parsed, "CFG_TMODE_MODE", 0))
        mode = _TMODE_MODE_NAMES.get(mode_raw, BaseMode.DISABLED)

        pos_type_raw = int(getattr(parsed, "CFG_TMODE_POS_TYPE", 1))
        # CFG_TMODE_FIXED_POS_ACC is in 0.1 mm units on the wire —
        # divide by 10 to surface mm at the Python API boundary
        # (matches CurrentBaseConfig.accuracy_mm units).
        acc_raw = int(getattr(parsed, "CFG_TMODE_FIXED_POS_ACC", 0))
        acc_mm = acc_raw // 10

        if pos_type_raw == 0:
            # ECEF mode — convert to LLH for display
            ecef_x_cm = int(getattr(parsed, "CFG_TMODE_ECEF_X", 0))
            ecef_y_cm = int(getattr(parsed, "CFG_TMODE_ECEF_Y", 0))
            ecef_z_cm = int(getattr(parsed, "CFG_TMODE_ECEF_Z", 0))
            ecef_x_hp = int(getattr(parsed, "CFG_TMODE_ECEF_X_HP", 0))
            ecef_y_hp = int(getattr(parsed, "CFG_TMODE_ECEF_Y_HP", 0))
            ecef_z_hp = int(getattr(parsed, "CFG_TMODE_ECEF_Z_HP", 0))

            # cm → m, HP is in 0.1mm = 0.0001m
            x_m = ecef_x_cm / 100.0 + ecef_x_hp * 0.0001
            y_m = ecef_y_cm / 100.0 + ecef_y_hp * 0.0001
            z_m = ecef_z_cm / 100.0 + ecef_z_hp * 0.0001

            lat, lon, alt_m = UbloxDriver._ecef_to_llh(x_m, y_m, z_m)
            pos_type = "ecef"
        else:
            # LLH mode — direct lat/lon/height
            lat_raw = int(getattr(parsed, "CFG_TMODE_LAT", 0))
            lon_raw = int(getattr(parsed, "CFG_TMODE_LON", 0))
            height_cm = int(getattr(parsed, "CFG_TMODE_HEIGHT", 0))
            lat = lat_raw * 1e-7
            lon = lon_raw * 1e-7
            alt_m = height_cm / 100.0
            pos_type = "llh"

        return CurrentBaseConfig(
            mode=mode,
            pos_type=pos_type,
            latitude=lat,
            longitude=lon,
            altitude_m=alt_m,
            accuracy_mm=acc_mm,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _poll_mon_ver(self) -> DeviceInfo:
        """Poll MON-VER and parse device identity.

        Uses a wall-clock timeout (``CONNECT_TIMEOUT``) to fail fast
        when the baud rate is wrong and the device returns only garbage.
        Also checks ``_cancel_event`` each iteration so the UI can
        abort a stuck connect.
        """
        ser, reader = self._require_connection()

        poll_msg = UBXMessage("MON", "MON-VER", POLL)
        ser.reset_input_buffer()
        ser.write(poll_msg.serialize())

        sw_version_str = ""
        fwver = ""
        protocol = ""
        hardware = ""
        mod = ""
        explicit_model = ""

        deadline = time.monotonic() + self.CONNECT_TIMEOUT

        for _ in range(_MAX_READ_ATTEMPTS):
            # Check cancel
            if self._cancel_event.is_set():
                raise ConnectionError("Connection cancelled")

            # Check wall-clock timeout
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"No response from device within {self.CONNECT_TIMEOUT:.0f}s "
                    "— check baud rate"
                )

            try:
                raw, parsed = reader.read()  # type: ignore[misc]
                if parsed is not None and hasattr(parsed, "identity"):
                    if parsed.identity == "MON-VER":
                        sw_raw = getattr(parsed, "swVersion", b"")
                        hw_raw = getattr(parsed, "hwVersion", b"")

                        if isinstance(sw_raw, bytes):
                            sw_version_str = sw_raw.decode(
                                "ascii", errors="replace"
                            ).strip("\x00 ")
                        else:
                            sw_version_str = str(sw_raw).strip("\x00 ")

                        if isinstance(hw_raw, bytes):
                            hardware = hw_raw.decode("ascii", errors="replace").strip(
                                "\x00 "
                            )
                        else:
                            hardware = str(hw_raw).strip("\x00 ")

                        # Parse extensions — pyubx2 uses 1-indexed names
                        # (extension_01, extension_02, ...) and bytes values.
                        # Also try 0-indexed for compatibility with mocks.
                        missed = 0
                        for i in range(30):
                            ext = getattr(parsed, f"extension_{i:02d}", None)
                            if ext is None:
                                missed += 1
                                if missed > 2:
                                    break  # stop after 2 consecutive misses
                                continue
                            missed = 0

                            if isinstance(ext, bytes):
                                ext_str = (
                                    ext.replace(b"\x00", b"")
                                    .decode("ascii", errors="replace")
                                    .strip()
                                )
                            else:
                                ext_str = str(ext).strip("\x00 ")

                            if "FWVER=" in ext_str:
                                # e.g. "FWVER=HPG 1.32" → "HPG 1.32"
                                fwver = ext_str.split("=", 1)[1].strip()
                            elif "PROTVER=" in ext_str:
                                # e.g. "PROTVER=27.31" → "27.31"
                                protocol = ext_str.split("=", 1)[1].strip()
                            elif "MOD=" in ext_str:
                                mod = ext_str.split("=", 1)[1].strip()
                            elif any(
                                m in ext_str
                                for m in ("ZED-", "NEO-", "MAX-", "SAM-", "LEA-")
                            ):
                                explicit_model = ext_str.strip()

                        # Firmware: prefer FWVER (HPG version), fallback to swVersion
                        firmware = fwver if fwver else sw_version_str

                        # Confidence-tiered identity resolution — see
                        # models.hardware_identity for the tier ladder.
                        # A guessed model (tiers 4-5, `inferred`) is never
                        # indistinguishable from a real read (tiers 1-3,
                        # `confirmed`): both are surfaced, but only a
                        # `confirmed` target can unlock a specific-model
                        # profile match.
                        identity = resolve_hardware_identity(
                            mod=mod,
                            explicit_model=explicit_model,
                            hw_version=hardware,
                            firmware=firmware,
                            protocol_version=protocol,
                        )
                        model = (
                            identity.target if identity.is_specific_model else "Unknown"
                        )

                        return DeviceInfo(
                            vendor="u-blox",
                            model=model,
                            firmware_version=firmware,
                            protocol_version=protocol,
                            hardware_version=hardware,
                            hardware_target=identity.target,
                            hardware_confidence=identity.confidence,
                        )
            except Exception:
                continue

        raise TimeoutError("No MON-VER response from device")

    def _send_cfg_valset(
        self,
        cfg_data: list[tuple[str, int]],
        layer: int = 1,
    ) -> None:
        """Send a CFG-VALSET message and wait for ACK.

        Public/legacy entrypoint — acquires ``self._lock`` to serialise
        with concurrent NAV/CFG/MON polls.  Prefer the internal
        ``_send_cfg_valset_locked`` from contexts that already hold
        the lock (e.g. ``configure_survey_in`` does two VALSETs as one
        atomic operation).

        Args:
            cfg_data: List of (key_name, value) tuples.
            layer: Configuration layer (1=RAM, 2=BBR, 4=Flash, 7=all).
        """
        with self._lock:
            self._send_cfg_valset_locked(cfg_data, layer=layer)

    def _send_cfg_valset_locked(
        self,
        cfg_data: list[tuple[str, int]],
        layer: int = 1,
    ) -> None:
        """Send a CFG-VALSET message and wait for ACK (must hold lock).

        Drains the serial RX buffer immediately before writing so the
        ACK isn't buried behind RTCM/NAV-PVT traffic that a busy base
        station receiver continuously streams.  Without this drain
        ``_wait_for_ack``'s 50-iteration cap can expire before the
        ACK is reached, producing spurious ``RuntimeError("No
        ACK/NAK response …")`` failures while the receiver actually
        applied the config.  See memory-bank/progress.md 2026-05-27
        "Cancel Survey-In doesn't cancel" entry.
        """
        ser, _ = self._require_connection()

        cfg_data_any: list[tuple[str | int, object]] = list(cfg_data)  # type: ignore[arg-type]
        msg = UBXMessage.config_set(layer, 0, cfg_data_any)
        ser.reset_input_buffer()
        ser.write(msg.serialize())  # type: ignore[union-attr]
        self._wait_for_ack("CFG-VALSET")

    def _wait_for_ack(self, expected_msg: str) -> None:
        """Read UBX messages until ACK-ACK or ACK-NAK is received.

        Args:
            expected_msg: Description for error messages.

        Raises:
            RuntimeError: If NAK received or timeout.
        """
        _, reader = self._require_connection()

        for _ in range(_MAX_READ_ATTEMPTS):
            try:
                raw, parsed = reader.read()  # type: ignore[misc]
                if parsed is None:
                    continue
                identity = getattr(parsed, "identity", "")
                if identity == "ACK-ACK":
                    return
                if identity == "ACK-NAK":
                    raise RuntimeError(f"Device rejected {expected_msg} (NAK)")
            except RuntimeError:
                raise
            except Exception:
                continue

        raise RuntimeError(f"No ACK/NAK response for {expected_msg}")
