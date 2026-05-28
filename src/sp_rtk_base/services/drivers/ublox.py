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
    FixedBaseConfig,
    GnssConfig,
    GnssConstellation,
    GnssSystemConfig,
    GpsFixType,
    GpsPosition,
    RtcmMessageConfig,
    RtcmOutputPort,
    RtcmPortConfig,
    SurveyInConfig,
    SurveyInProgress,
)
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

# RTCM message IDs and their CFG key base names.
# Most follow pattern CFG_MSGOUT_RTCM_3X_TYPE{id}_{port}
# except 4072 which is CFG_MSGOUT_RTCM_3X_TYPE4072_0_{port}
_RTCM_KEY_BASES: dict[int, str] = {
    1005: "CFG_MSGOUT_RTCM_3X_TYPE1005",
    1074: "CFG_MSGOUT_RTCM_3X_TYPE1074",
    1077: "CFG_MSGOUT_RTCM_3X_TYPE1077",
    1084: "CFG_MSGOUT_RTCM_3X_TYPE1084",
    1087: "CFG_MSGOUT_RTCM_3X_TYPE1087",
    1094: "CFG_MSGOUT_RTCM_3X_TYPE1094",
    1097: "CFG_MSGOUT_RTCM_3X_TYPE1097",
    1124: "CFG_MSGOUT_RTCM_3X_TYPE1124",
    1127: "CFG_MSGOUT_RTCM_3X_TYPE1127",
    1230: "CFG_MSGOUT_RTCM_3X_TYPE1230",
    4072: "CFG_MSGOUT_RTCM_3X_TYPE4072_0",
}


def _rtcm_key(msg_id: int, port: str) -> str:
    """Build the full CFG key name for an RTCM message + port."""
    base = _RTCM_KEY_BASES.get(msg_id, f"CFG_MSGOUT_RTCM_3X_TYPE{msg_id}")
    return f"{base}_{port}"


# Legacy USB-only mapping (backward compat)
_RTCM_USB_KEYS: dict[int, str] = {
    msg_id: _rtcm_key(msg_id, "USB") for msg_id in _RTCM_KEY_BASES
}


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

    # How long to wait after a UBX-CFG-RST controlled-GNSS-start for
    # the receiver's GNSS subsystem to come back up before issuing
    # further commands.  Empirically ~1-3 s on ZED-F9P.  3 s gives
    # margin without making the survey-start UX noticeably slower.
    _CFG_RST_SETTLE_S: float = 3.0

    # Maximum ``NAV-SVIN.dur`` allowed at the first verify poll for
    # a survey-in start to be considered "fresh".  ``dur`` is a
    # BBR-backed accumulator on the ZED-F9P; if the CFG-RST didn't
    # actually clear it (a hardware-level failure we cannot fix from
    # software), the floor check fires a clear, actionable error
    # instead of letting the UI display a stale 17-hour duration.
    _SVIN_DUR_FLOOR_S: int = 30

    def configure_survey_in(self, config: SurveyInConfig) -> None:
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
            # Step 1: clear the BBR-backed survey-in accumulator.
            # CFG-VALSET writes alone (even to layer=7) do NOT reset
            # NAV-SVIN.dur on HPG 1.12 — we verified empirically that
            # ``dur`` ticks continuously across host restarts and
            # every TMODE_MODE=0 write we tried.  Only UBX-CFG-RST
            # (controlled GNSS start, position BBR bit) actually
            # zeroes the accumulator.  See ``reset_survey_state``.
            self._reset_survey_state_locked()

            # Step 2: full-layer TMODE disable.  Belt-and-suspenders
            # alongside the CFG-RST above: per u-blox's own C099
            # "F9P Base Survey in disable.txt" script, the canonical
            # disable writes to all three layers (1|2|4 = 7) so any
            # TMODE config from a prior session is wiped consistently.
            # Flashed ECEF/LLH coordinates from a completed prior
            # survey persist — only the MODE key is touched, so the
            # operator can still switch back to a known fixed-base
            # position manually via Restore.
            self._send_cfg_valset_locked(
                [("CFG_TMODE_MODE", 0)], layer=self._TMODE_DISABLE_ALL_LAYERS
            )

            # Step 3: settle.  The ZED-F9P needs a brief quiet period
            # between TMODE-disable and TMODE-enable VALSETs so the
            # 0 -> 1 edge is registered as a fresh survey-in request
            # rather than coalesced with the previous state.
            time.sleep(self._TMODE_RESTART_DELAY_S)

            # Step 4: write the new survey-in parameters and enable
            # to RAM only.  Per u-blox C099 "F9P Base Survey in
            # start.txt", survey-in is intentionally NOT persisted to
            # flash — only the completed fixed-base coordinates from
            # ``save_to_flash`` are persisted.
            cfg_data = [
                ("CFG_TMODE_SVIN_MIN_DUR", config.min_duration_seconds),
                ("CFG_TMODE_SVIN_ACC_LIMIT", config.accuracy_limit_mm),
                # Survey-in mode (last so params land first)
                ("CFG_TMODE_MODE", 1),
            ]
            self._send_cfg_valset_locked(cfg_data, layer=1)  # RAM only

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
                        "Survey-in start failed: the receiver's "
                        "NAV-SVIN duration accumulator did not reset "
                        f"(reported {before.duration_seconds}s at "
                        f"start, expected < {self._SVIN_DUR_FLOOR_S}s "
                        "after CFG-RST).  This typically means the "
                        "receiver firmware (HPG 1.12) requires a "
                        "physical power cycle to clear stuck state.  "
                        "Unplug and replug the GPS USB cable, then "
                        "try again.  TMODE has been reset to 0."
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

        cfg_data = [
            ("CFG_TMODE_MODE", 2),  # Fixed mode
            ("CFG_TMODE_POS_TYPE", 1),  # LLH
            ("CFG_TMODE_LAT", lat_hp),
            ("CFG_TMODE_LON", lon_hp),
            ("CFG_TMODE_HEIGHT", alt_cm),
            ("CFG_TMODE_FIXED_POS_ACC", config.accuracy_mm),
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
            self._reset_survey_state_locked()
            self._send_cfg_valset_locked(
                [("CFG_TMODE_MODE", 0)], layer=self._TMODE_DISABLE_ALL_LAYERS
            )
            time.sleep(self._TMODE_RESTART_DELAY_S)
            self._send_cfg_valset_locked(cfg_data, layer=1)  # RAM only
        logger.info(
            "Fixed base configured: %.7f, %.7f, %.2fm",
            config.latitude,
            config.longitude,
            config.altitude_m,
        )

    def configure_rtcm_messages(self, config: RtcmMessageConfig) -> None:
        cfg_data: list[tuple[str, int]] = []

        # First disable all known RTCM messages
        for key_name in _RTCM_USB_KEYS.values():
            cfg_data.append((key_name, 0))

        # Then enable requested messages at the specified rate
        for msg_id in config.message_ids:
            # New local name (different type from the str loop var above)
            mapped_key = _RTCM_USB_KEYS.get(msg_id)
            if mapped_key is not None:
                cfg_data.append((mapped_key, config.rate_hz))
            else:
                logger.warning("Unknown RTCM message ID %d — skipped", msg_id)

        with self._lock:
            self._send_cfg_valset_locked(cfg_data, layer=1)  # RAM only
        logger.info(
            "RTCM messages configured: %s @ %dHz",
            config.message_ids,
            config.rate_hz,
        )

    def get_rtcm_config(self) -> RtcmMessageConfig:
        """Read the current RTCM USB output configuration from the receiver.

        Polls ``CFG_MSGOUT_RTCM_3X_TYPE*_USB`` keys and returns which
        messages are enabled (rate > 0) and the most common rate.
        """
        with self._lock:
            return self._get_rtcm_config_locked()

    def _get_rtcm_config_locked(self) -> RtcmMessageConfig:
        """Read RTCM config (must hold self._lock)."""
        ser, reader = self._require_connection()

        keys: list[str | int] = list(_RTCM_USB_KEYS.values())
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
                    return self._parse_rtcm_valget(parsed)
            except Exception:
                continue

        raise RuntimeError("No CFG-VALGET response for RTCM config")

    @staticmethod
    def _parse_rtcm_valget(parsed: object) -> RtcmMessageConfig:
        """Parse a CFG-VALGET response containing RTCM message rates."""
        enabled_ids: list[int] = []
        rates: list[int] = []

        for msg_id, key_name in _RTCM_USB_KEYS.items():
            rate = int(getattr(parsed, key_name, 0))
            if rate > 0:
                enabled_ids.append(msg_id)
                rates.append(rate)

        # Use the most common rate, defaulting to 1
        common_rate = max(set(rates), key=rates.count) if rates else 1

        return RtcmMessageConfig(
            message_ids=enabled_ids,
            rate_hz=common_rate,
        )

    # ------------------------------------------------------------------
    # Multi-port RTCM configuration
    # ------------------------------------------------------------------

    def get_rtcm_port_config(self) -> RtcmPortConfig:
        """Read RTCM output config for ALL ports from the receiver.

        Polls ``CFG_MSGOUT_RTCM_3X_TYPE*_{USB,UART1,UART2,I2C,SPI}``
        and returns a matrix of msg_id → {port: rate}.
        """
        with self._lock:
            return self._get_rtcm_port_config_locked()

    def _get_rtcm_port_config_locked(self) -> RtcmPortConfig:
        """Read multi-port RTCM config (must hold self._lock)."""
        ser, reader = self._require_connection()

        # Build key list: 11 messages × 5 ports = 55 keys
        all_keys: list[str | int] = []
        for msg_id in _ALL_RTCM_IDS:
            for port in _RTCM_PORTS:
                all_keys.append(_rtcm_key(msg_id, port))

        msg = UBXMessage.config_poll(0, 0, all_keys)
        ser.reset_input_buffer()
        ser.write(msg.serialize())  # type: ignore[union-attr]

        for _ in range(_MAX_READ_ATTEMPTS):
            try:
                raw, parsed = reader.read()  # type: ignore[misc]
                if parsed is None:
                    continue
                identity = getattr(parsed, "identity", "")
                if identity == "CFG-VALGET":
                    return self._parse_rtcm_port_valget(parsed)
            except Exception:
                continue

        raise RuntimeError("No CFG-VALGET response for RTCM port config")

    @staticmethod
    def _parse_rtcm_port_valget(parsed: object) -> RtcmPortConfig:
        """Parse a CFG-VALGET response into a multi-port RTCM config."""
        messages: dict[int, dict[str, int]] = {}

        for msg_id in _ALL_RTCM_IDS:
            port_rates: dict[str, int] = {}
            for port in _RTCM_PORTS:
                key = _rtcm_key(msg_id, port)
                rate = int(getattr(parsed, key, 0))
                port_rates[port] = rate
            messages[msg_id] = port_rates

        return RtcmPortConfig(messages=messages)

    def configure_rtcm_ports(self, config: RtcmPortConfig) -> None:
        """Apply multi-port RTCM output configuration to the receiver.

        Sends a CFG-VALSET with rates for each message on each port.
        """
        cfg_data: list[tuple[str, int]] = []

        for msg_id, port_rates in config.messages.items():
            if msg_id not in _RTCM_KEY_BASES:
                logger.warning("Unknown RTCM message ID %d — skipped", msg_id)
                continue
            for port, rate in port_rates.items():
                key = _rtcm_key(msg_id, port)
                cfg_data.append((key, rate))

        if not cfg_data:
            logger.warning("No valid RTCM port config to apply")
            return

        with self._lock:
            self._send_cfg_valset_locked(cfg_data, layer=1)  # RAM only
        logger.info("RTCM multi-port config applied (%d keys)", len(cfg_data))

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

    _GNSS_ID_REVERSE: dict[GnssConstellation, int] = {
        v: k for k, v in _GNSS_ID_MAP.items()
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

    def configure_gnss(self, config: GnssConfig) -> None:
        """Send CFG-GNSS to configure constellation selection."""
        # Build CFG-GNSS SET message
        # We need numTrkChHw, numTrkChUse, numConfigBlocks + per-system data
        num_blocks = len(config.systems)

        # Build kwargs for UBXMessage
        kwargs: dict[str, int] = {
            "msgVer": 0,
            "numTrkChHw": 0,  # read-only, set to 0
            "numTrkChUse": 0xFF,  # use max available
            "numConfigBlocks": num_blocks,
        }

        for i, sys_cfg in enumerate(config.systems):
            # pyubx2 always uses 1-indexed suffixes: _01, _02, ...
            suffix = f"_{i + 1:02d}"
            gnss_id = self._GNSS_ID_REVERSE.get(sys_cfg.constellation, 0)
            flags = (1 if sys_cfg.enabled else 0) | (sys_cfg.sig_cfg_mask << 16)

            kwargs[f"gnssId{suffix}"] = gnss_id
            kwargs[f"resTrkCh{suffix}"] = sys_cfg.min_channels
            kwargs[f"maxTrkCh{suffix}"] = sys_cfg.max_channels
            kwargs[f"flags{suffix}"] = flags

        msg = UBXMessage("CFG", "CFG-GNSS", SET, **kwargs)  # type: ignore[arg-type]
        with self._lock:
            ser, _ = self._require_connection()
            ser.reset_input_buffer()
            ser.write(msg.serialize())
            self._wait_for_ack("CFG-GNSS")

        enabled = config.enabled_constellations()
        logger.info(
            "GNSS constellations configured: %s",
            [c.value for c in enabled],
        )

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

    def reset_survey_state(self) -> None:
        """Reset the receiver's BBR-backed survey-in accumulator.

        Sends UBX-CFG-RST with ``resetMode=0x09`` (controlled GNSS
        start) and the ``pos`` BBR bit set.  This causes the receiver
        to clear its last-position estimate AND the survey-in state
        machine (``NAV-SVIN.dur`` / ``obs``).  Ephemeris and almanac
        are preserved so GPS re-acquires within ~5-30 s (warmstart
        equivalent — not a full coldstart).

        Why this exists: on ZED-F9P firmware HPG 1.12 the survey-in
        ``dur`` accumulator is BBR-backed and is NOT cleared by any
        CFG-VALSET write, including ``CFG_TMODE_MODE=0`` to layer=7
        (RAM+BBR+Flash).  Verified empirically on larson-base:
        ``dur`` accumulated to ~62000 s (~17 h) across multiple
        host restarts and TMODE writes.  Only CFG-RST resets it.

        Persisted base-station coordinates in Flash are untouched —
        Flash is separate from BBR.
        """
        with self._lock:
            self._reset_survey_state_locked()

    def _reset_survey_state_locked(self) -> None:
        """CFG-RST helper (must hold self._lock).

        Does NOT wait for ACK — the receiver may reset before it can
        send one, depending on ``resetMode``.  Instead we sleep for
        ``_CFG_RST_SETTLE_S`` to give the GNSS subsystem time to
        come back up.
        """
        ser, _ = self._require_connection()
        msg = UBXMessage(  # type: ignore[misc]
            "CFG",
            "CFG-RST",
            SET,
            pos=1,
            resetMode=0x09,
        )
        ser.reset_input_buffer()
        ser.write(msg.serialize())  # type: ignore[union-attr]
        time.sleep(self._CFG_RST_SETTLE_S)
        logger.info(
            "Sent UBX-CFG-RST (controlled GNSS start, pos bit) — "
            "survey accumulator cleared; receiver re-acquiring GNSS"
        )

    def send_cfg_rst_diagnostic(
        self,
        reset_mode: int,
        wait_seconds: float,
        bbr_bits: dict[str, int],
    ) -> tuple[SurveyInProgress, SurveyInProgress, bytes]:
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
                reading the after-state.
            bbr_bits: Named BBR-clear bits, e.g. ``{"pos": 1,
                "eph": 0}``.  Passed straight to the ``UBXMessage``
                constructor.  Unknown keys raise.

        Returns:
            Tuple of ``(before, after, ubx_bytes_sent)`` — both
            ``before`` and ``after`` are ``SurveyInProgress`` reads of
            NAV-SVIN; ``ubx_bytes_sent`` is the serialised UBX frame
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
            time.sleep(wait_seconds)
            after = self._get_survey_in_locked()
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
        _MODE_MAP: dict[int, BaseMode] = {
            0: BaseMode.DISABLED,
            1: BaseMode.SURVEY_IN,
            2: BaseMode.FIXED,
        }
        mode = _MODE_MAP.get(mode_raw, BaseMode.DISABLED)

        pos_type_raw = int(getattr(parsed, "CFG_TMODE_POS_TYPE", 1))
        acc_raw = int(getattr(parsed, "CFG_TMODE_FIXED_POS_ACC", 0))

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
            accuracy_mm=acc_raw,
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

        model = "Unknown"
        sw_version_str = ""
        fwver = ""
        protocol = ""
        hardware = ""

        # Hardware version → model lookup for common u-blox receivers
        _hw_model_map: dict[str, str] = {
            "00190000": "ZED-F9P",
            "001B0000": "ZED-F9R",
            "00180000": "NEO-M9N",
        }

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
                                model = ext_str.split("=", 1)[1].strip()
                            elif any(
                                m in ext_str
                                for m in ("ZED-", "NEO-", "MAX-", "SAM-", "LEA-")
                            ):
                                model = ext_str.strip()

                        # Firmware: prefer FWVER (HPG version), fallback to swVersion
                        firmware = fwver if fwver else sw_version_str

                        # Fallback: infer model from hardware version
                        if model == "Unknown" and hardware in _hw_model_map:
                            model = _hw_model_map[hardware]

                        # Fallback: infer from firmware string
                        if model == "Unknown":
                            if "HPG" in firmware:
                                model = "ZED-F9P"
                            elif "ADR" in firmware:
                                model = "ZED-F9R"

                        return DeviceInfo(
                            vendor="u-blox",
                            model=model,
                            firmware_version=firmware,
                            protocol_version=protocol,
                            hardware_version=hardware,
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
