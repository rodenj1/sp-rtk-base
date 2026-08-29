"""Device service — GPS receiver connection and configuration management.

Orchestrates GPS receiver drivers, tracks connection state, and
enforces mutual exclusion with the relay service (serial port handoff).

The DeviceService is **entirely optional** — the relay can operate
without a GPS device (e.g. TCP input from a remote receiver).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Protocol

from sp_rtk_base.models.device_models import (
    ApplyConfigCellDiff,
    ApplyConfigResult,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceConnectionState,
    DeviceInfo,
    DeviceStatus,
    FixedBaseConfig,
    GnssConfig,
    GpsPosition,
    PortId,
    PortProtocolConfig,
    RtcmMessageConfig,
    RtcmPortConfig,
    RtcmRowId,
    SurveyInConfig,
    SurveyInProgress,
    UbxProtocol,
)
from sp_rtk_base.models.device_models import (
    BaseMode as TmodeMode,
)
from sp_rtk_base.models.profile_models import ReceiverConfig
from sp_rtk_base.services.drivers.base import GpsReceiverDriver

logger = logging.getLogger(__name__)


class ApplyConfigRefusedError(Exception):
    """Business-rule refusal for ``apply_receiver_config`` — device-state guards.

    Raised before any write when a live-receiver-state precondition
    isn't met (issue #61): the UBX-in liveness guard or the
    ``tmode_mode: fixed`` coordinate guard. Carries the failed rule
    name so the API can surface it in a 400 response.
    """

    def __init__(self, rule: str, message: str) -> None:
        self.rule = rule
        super().__init__(message)


# ---------------------------------------------------------------------------
# Non-blocking RTCM throughput estimate (issue #61)
#
# Approximate typical RTCM3 frame sizes in bytes at a moderate satellite
# count. These are estimates for a heads-up warning only — not exact
# wire measurements — so precision beyond "roughly right" isn't the
# goal. MSM4/MSM7 sizes scale with tracked satellite count; the values
# below assume a typical 8-12 satellites per constellation.
# ---------------------------------------------------------------------------

_APPROX_RTCM_FRAME_BYTES: dict[RtcmRowId, int] = {
    RtcmRowId.RTCM_1005: 19,
    RtcmRowId.RTCM_4072_0: 72,
    RtcmRowId.RTCM_4072_1: 40,
    RtcmRowId.RTCM_1074: 150,
    RtcmRowId.RTCM_1077: 300,
    RtcmRowId.RTCM_1084: 130,
    RtcmRowId.RTCM_1087: 260,
    RtcmRowId.RTCM_1094: 120,
    RtcmRowId.RTCM_1097: 240,
    RtcmRowId.RTCM_1124: 130,
    RtcmRowId.RTCM_1127: 260,
    RtcmRowId.RTCM_1230: 25,
}

# 8N1 serial framing: ~10 bits on the wire per payload byte (1 start +
# 8 data + 1 stop), so baud / 10 approximates byte capacity per second.
_BITS_PER_BYTE_ON_WIRE = 10.0

# Warn once estimated RTCM throughput crosses this fraction of a
# data-link port's baud capacity.
_THROUGHPUT_WARN_THRESHOLD = 0.70


def _throughput_warnings(
    config: ReceiverConfig, baud_rates: dict[PortId, int]
) -> list[str]:
    """Non-blocking advisories when estimated RTCM output nears port capacity.

    Takes the whole ``ReceiverConfig`` rather than its three relevant
    fields individually — ``rtcm_stream.matrix``, ``data_link_port``
    and ``meas_period_ms`` only ever travel together for this check,
    and the config already bundles them.
    """
    matrix = config.rtcm_stream.matrix
    hz = 1000.0 / config.meas_period_ms
    warnings: list[str] = []
    for port in config.data_link_port:
        baud = baud_rates.get(port)
        if not baud:
            continue
        bytes_per_sec = (
            sum(
                _APPROX_RTCM_FRAME_BYTES.get(row, 0)
                for row, ports in matrix.items()
                if ports.get(port, False)
            )
            * hz
        )
        capacity_bytes_per_sec = baud / _BITS_PER_BYTE_ON_WIRE
        fraction = bytes_per_sec / capacity_bytes_per_sec
        if fraction > _THROUGHPUT_WARN_THRESHOLD:
            warnings.append(
                f"Estimated RTCM throughput on {port.value} is "
                f"~{round(fraction * 100)}% of its {baud} baud capacity "
                "— consider a higher baud rate or fewer messages."
            )
    return warnings


class DeviceService:
    """Manages GPS receiver connection and configuration lifecycle.

    The service is driver-agnostic — it delegates all vendor-specific
    I/O to a :class:`GpsReceiverDriver` implementation. The UI and
    API layers interact only with this service.

    Key responsibilities:
    - Connection lifecycle (connect / disconnect)
    - State tracking (disconnected → connecting → connected → configuring)
    - Capability queries (what can this device do?)
    - Mutual exclusion with relay (device config and relay are mutually exclusive)
    - Async wrappers around synchronous driver methods
    """

    def __init__(self) -> None:
        self._driver: GpsReceiverDriver | None = None
        self._state = DeviceConnectionState.DISCONNECTED
        self._port: str | None = None
        self._baud_rate: int | None = None
        self._info: DeviceInfo | None = None
        self._last_error: str | None = None
        self._connected_at: datetime | None = None
        self._relay_running_check: _RelayRunningCheck | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> DeviceConnectionState:
        """Current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Whether a GPS device is currently connected."""
        return self._state == DeviceConnectionState.CONNECTED

    @property
    def is_available(self) -> bool:
        """Whether a driver is loaded (device management is possible)."""
        return self._driver is not None

    @property
    def driver(self) -> GpsReceiverDriver | None:
        """The currently loaded driver, or None."""
        return self._driver

    @property
    def capabilities(self) -> set[DeviceCapability]:
        """Capabilities of the loaded driver (empty if no driver)."""
        if self._driver is None:
            return set()
        return self._driver.get_capabilities()

    @property
    def device_info(self) -> DeviceInfo | None:
        """Device identity from last successful connect."""
        return self._info

    # ------------------------------------------------------------------
    # Driver management
    # ------------------------------------------------------------------

    def set_driver(self, driver: GpsReceiverDriver) -> None:
        """Load a GPS receiver driver.

        Args:
            driver: Concrete driver instance.

        Raises:
            RuntimeError: If a device is currently connected.
        """
        if self._state not in (
            DeviceConnectionState.DISCONNECTED,
            DeviceConnectionState.ERROR,
        ):
            raise RuntimeError(
                "Cannot change driver while connected — disconnect first"
            )
        self._driver = driver
        logger.info("Device driver loaded: %s", driver.vendor_name)

    def set_relay_check(self, check: _RelayRunningCheck) -> None:
        """Set a callback to check if the relay is running.

        Used for mutual exclusion — device config requires relay stopped.

        Args:
            check: Callable that returns True if relay is running.
        """
        self._relay_running_check = check

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, port: str, baud_rate: int = 115200) -> DeviceInfo:
        """Connect to a GPS receiver on the given serial port.

        Args:
            port: Serial port path (e.g. ``/dev/ttyACM0``).
            baud_rate: Serial baud rate.

        Returns:
            Device identity information.

        Raises:
            RuntimeError: If no driver loaded, already connected, or relay is running.
            ConnectionError: If connection fails.
            TimeoutError: If device does not respond.
        """
        # These early-return paths used to ``raise`` without first
        # transitioning ``self._state`` away from CONNECTING (the UI
        # calls ``set_connecting()`` before us).  The Survey page
        # then sat on "Connecting..." forever because the except
        # branch below — which DOES set state=ERROR — was never
        # reached.  Reset state on each early-out so the UI's
        # ``_update_ui_state`` reflects reality.
        if self._driver is None:
            self._state = DeviceConnectionState.DISCONNECTED
            self._last_error = "No GPS driver loaded"
            raise RuntimeError("No GPS driver loaded")

        if self._state == DeviceConnectionState.CONNECTED:
            # Don't clobber the CONNECTED state — the caller's
            # request was a no-op (or worse, a logic error on their
            # side).  Just raise.
            raise RuntimeError("Already connected — disconnect first")

        if self._relay_running_check is not None and self._relay_running_check():
            self._state = DeviceConnectionState.ERROR
            self._last_error = (
                "Cannot connect to device while relay is running — stop relay first"
            )
            raise RuntimeError(self._last_error)

        self._state = DeviceConnectionState.CONNECTING
        self._last_error = None

        try:
            info = await asyncio.to_thread(self._driver.connect, port, baud_rate)
            self._state = DeviceConnectionState.CONNECTED
            self._port = port
            self._baud_rate = baud_rate
            self._info = info
            self._connected_at = datetime.now(tz=timezone.utc)
            logger.info(
                "Connected to %s %s on %s",
                info.vendor,
                info.model,
                port,
            )
            return info
        except Exception as exc:
            self._state = DeviceConnectionState.ERROR
            self._last_error = str(exc)
            logger.error("Failed to connect to %s: %s", port, exc)
            raise

    def set_connecting(self) -> None:
        """Set state to CONNECTING for UI feedback before connect."""
        self._state = DeviceConnectionState.CONNECTING
        self._last_error = None

    def cancel_connect(self) -> None:
        """Cancel an in-progress connect attempt.

        Safe to call even when not connecting — will be a no-op.
        """
        if self._driver is not None and hasattr(self._driver, "cancel_connect"):
            self._driver.cancel_connect()  # type: ignore[attr-defined]
        self._state = DeviceConnectionState.DISCONNECTED
        self._last_error = "Connection cancelled"
        logger.info("Connect cancelled")

    async def disconnect(self) -> None:
        """Disconnect from the GPS receiver.

        Safe to call when already disconnected.
        """
        if self._driver is not None and self._driver.is_connected:
            try:
                await asyncio.to_thread(self._driver.disconnect)
            except Exception:
                logger.exception("Error during disconnect")

        self._state = DeviceConnectionState.DISCONNECTED
        self._port = None
        self._baud_rate = None
        self._info = None
        self._connected_at = None
        self._last_error = None
        logger.info("Device disconnected")

    # ------------------------------------------------------------------
    # Configuration commands
    # ------------------------------------------------------------------

    def _require_connected(self) -> GpsReceiverDriver:
        """Ensure device is connected and return the driver.

        Raises:
            RuntimeError: If not connected or relay is running.
        """
        if self._driver is None:
            raise RuntimeError("No GPS driver loaded")
        if self._state != DeviceConnectionState.CONNECTED:
            raise RuntimeError("Device not connected")
        if self._relay_running_check is not None and self._relay_running_check():
            raise RuntimeError(
                "Cannot configure device while relay is running — stop relay first"
            )
        return self._driver

    async def configure_survey_in(self, config: SurveyInConfig) -> None:
        """Configure the receiver for survey-in mode.

        On any failure, automatically hardware-resets and reconnects
        the receiver so the next Start attempt sees a clean state.
        The original exception is re-raised after the reset attempt
        (and the reset error is logged but not propagated, so the
        operator sees the actionable original error).

        Args:
            config: Survey-in parameters.

        Raises:
            RuntimeError: If not connected or relay is running.
        """
        driver = self._require_connected()
        self._state = DeviceConnectionState.CONFIGURING
        try:
            await asyncio.to_thread(driver.configure_survey_in, config)
            self._state = DeviceConnectionState.CONNECTED
            logger.info(
                "Survey-in configured: %ds min, %dmm accuracy",
                config.min_duration_seconds,
                config.accuracy_limit_mm,
            )
        except Exception as exc:
            self._state = DeviceConnectionState.CONNECTED
            self._last_error = str(exc)
            # Auto-reset on failure so the next Start sees a clean
            # receiver state.  Surface the original error (it
            # explains what went wrong); log a reset failure
            # separately if one occurs.
            if hasattr(driver, "reset_and_reconnect"):
                try:
                    await asyncio.to_thread(driver.reset_and_reconnect)  # type: ignore[attr-defined]
                    logger.info("Auto-reset receiver after configure_survey_in failure")
                except Exception:
                    logger.exception(
                        "Auto-reset after configure_survey_in failure "
                        "also failed — receiver may be in inconsistent "
                        "state; click Reset GPS manually"
                    )
            raise

    async def send_cfg_rst_diagnostic(
        self,
        reset_mode: int,
        wait_seconds: float,
        bbr_bits: dict[str, int],
        read_after_state: bool = True,
    ) -> tuple[SurveyInProgress, SurveyInProgress | None, bytes]:
        """Send an arbitrary UBX-CFG-RST and capture before/after state.

        Thin async wrapper around ``UbloxDriver.send_cfg_rst_diagnostic``
        for the ``POST /api/device/debug/cfg-rst`` endpoint.  Only
        works on real u-blox drivers; the fake driver does not expose
        this method.

        ``read_after_state=False`` skips the post-write NAV-SVIN poll
        — required for hardware resets (``resetMode=0`` / ``4``) that
        re-enumerate the USB port.

        Raises:
            RuntimeError: If not connected or the active driver does
                not support CFG-RST diagnostics.
        """
        driver = self._require_connected()
        if not hasattr(driver, "send_cfg_rst_diagnostic"):
            raise RuntimeError(
                "Active driver does not support CFG-RST diagnostics "
                "(only u-blox drivers do)."
            )
        return await asyncio.to_thread(
            driver.send_cfg_rst_diagnostic,  # type: ignore[attr-defined]
            reset_mode,
            wait_seconds,
            bbr_bits,
            read_after_state,
        )

    async def cancel_survey_in(self) -> None:
        """Cancel an in-progress survey-in by disabling TMODE.

        Sends ``CFG_TMODE_MODE=0`` then issues a hardware reset +
        reconnect so the receiver's BBR-backed survey accumulator
        is wiped.  Without the reset, the next Start would inherit
        the cancelled session's ``dur`` counter and the receiver
        would treat it as a continuation rather than a fresh start.

        Raises:
            RuntimeError: If not connected or relay is running.
        """
        driver = self._require_connected()
        self._state = DeviceConnectionState.CONFIGURING
        try:
            await asyncio.to_thread(driver.disable_base_mode)
            # Reset to clear the BBR survey accumulator so the next
            # Start sees a clean dur=0.  Without this, dur carries
            # over from the cancelled session and the receiver
            # treats subsequent surveys as continuations.
            if hasattr(driver, "reset_and_reconnect"):
                try:
                    await asyncio.to_thread(driver.reset_and_reconnect)  # type: ignore[attr-defined]
                    logger.info("Survey-in cancelled and receiver reset")
                except Exception:
                    logger.exception(
                        "TMODE was disabled but the post-cancel reset "
                        "failed — receiver may carry stale state into "
                        "the next survey"
                    )
            else:
                logger.info("Survey-in cancelled (TMODE disabled)")
            self._state = DeviceConnectionState.CONNECTED
        except Exception as exc:
            self._state = DeviceConnectionState.CONNECTED
            self._last_error = str(exc)
            raise

    async def reset_receiver(self) -> DeviceInfo:
        """Hardware-reset the receiver and reconnect on the same port.

        Wraps ``UbloxDriver.reset_and_reconnect`` (only u-blox drivers
        support this — see the docstring there for the full sequence
        and the rationale for hardware reset over software variants).

        Raises:
            RuntimeError: If not connected, no relay-mutex conflict,
                or the active driver doesn't support hardware reset.
        """
        driver = self._require_connected()
        if not hasattr(driver, "reset_and_reconnect"):
            raise RuntimeError(
                "Active driver does not support hardware reset "
                "(only u-blox drivers do)."
            )
        self._state = DeviceConnectionState.CONFIGURING
        try:
            info = await asyncio.to_thread(driver.reset_and_reconnect)  # type: ignore[attr-defined]
            self._state = DeviceConnectionState.CONNECTED
            self._last_error = None
            logger.info("Receiver hardware-reset and reconnected")
            return info  # type: ignore[no-any-return]
        except Exception as exc:
            self._state = DeviceConnectionState.DISCONNECTED
            self._last_error = str(exc)
            raise

    async def configure_fixed_base(self, config: FixedBaseConfig) -> None:
        """Configure the receiver for fixed-position mode.

        Args:
            config: Fixed base parameters.

        Raises:
            RuntimeError: If not connected or relay is running.
        """
        driver = self._require_connected()
        self._state = DeviceConnectionState.CONFIGURING
        try:
            await asyncio.to_thread(driver.configure_fixed_base, config)
            self._state = DeviceConnectionState.CONNECTED
            logger.info(
                "Fixed base configured: %.6f, %.6f, %.1fm",
                config.latitude,
                config.longitude,
                config.altitude_m,
            )
        except Exception as exc:
            self._state = DeviceConnectionState.CONNECTED
            self._last_error = str(exc)
            raise

    async def configure_rtcm_messages(self, config: RtcmMessageConfig) -> None:
        """Enable/disable RTCM message outputs.

        Args:
            config: RTCM message selection.

        Raises:
            RuntimeError: If not connected or relay is running.
        """
        driver = self._require_connected()
        self._state = DeviceConnectionState.CONFIGURING
        try:
            await asyncio.to_thread(driver.configure_rtcm_messages, config)
            self._state = DeviceConnectionState.CONNECTED
            logger.info(
                "RTCM messages configured: %s @ %dHz",
                config.message_id_values,
                config.rate_hz,
            )
        except Exception as exc:
            self._state = DeviceConnectionState.CONNECTED
            self._last_error = str(exc)
            raise

    async def get_rtcm_config(self) -> RtcmMessageConfig:
        """Read the current RTCM message output configuration.

        Returns:
            Current RTCM message selection and rate.

        Raises:
            RuntimeError: If not connected.
        """
        driver = self._require_connected()
        return await asyncio.to_thread(driver.get_rtcm_config)

    async def get_rtcm_port_config(self) -> RtcmPortConfig:
        """Read RTCM output config for all ports (USB, UART1, etc.).

        Returns:
            Per-message, per-port rate configuration.

        Raises:
            RuntimeError: If not connected.
        """
        driver = self._require_connected()
        return await asyncio.to_thread(driver.get_rtcm_port_config)

    async def configure_rtcm_ports(self, config: RtcmPortConfig) -> None:
        """Apply multi-port RTCM output configuration.

        Args:
            config: Per-message, per-port rate configuration.

        Raises:
            RuntimeError: If not connected or relay is running.
        """
        driver = self._require_connected()
        self._state = DeviceConnectionState.CONFIGURING
        try:
            await asyncio.to_thread(driver.configure_rtcm_ports, config)
            self._state = DeviceConnectionState.CONNECTED
            logger.info("Multi-port RTCM config applied")
        except Exception as exc:
            self._state = DeviceConnectionState.CONNECTED
            self._last_error = str(exc)
            raise

    async def get_port_protocols(self) -> PortProtocolConfig:
        """Read live in/out protocol state for UART1, UART2 and USB.

        Returns:
            Per-port enabled input/output protocols.

        Raises:
            RuntimeError: If not connected.
        """
        driver = self._require_connected()
        return await asyncio.to_thread(driver.get_port_protocols)

    async def get_gnss_config(self) -> GnssConfig:
        """Read the current GNSS constellation configuration.

        Returns:
            Current GNSS system configuration.

        Raises:
            RuntimeError: If not connected.
        """
        driver = self._require_connected()
        return await asyncio.to_thread(driver.get_gnss_config)

    async def get_base_config(self) -> CurrentBaseConfig:
        """Read the current base station configuration from the receiver.

        Returns:
            Current base mode and, for fixed mode, the coordinates.

        Raises:
            RuntimeError: If not connected.
        """
        driver = self._require_connected()
        return await asyncio.to_thread(driver.get_base_config)

    async def configure_gnss(self, config: GnssConfig) -> None:
        """Write GNSS constellation configuration to the receiver.

        Args:
            config: Desired GNSS system configuration.

        Raises:
            RuntimeError: If not connected or relay is running.
        """
        driver = self._require_connected()
        self._state = DeviceConnectionState.CONFIGURING
        try:
            await asyncio.to_thread(driver.configure_gnss, config)
            self._state = DeviceConnectionState.CONNECTED
            logger.info(
                "GNSS constellations configured: %s",
                [c.value for c in config.enabled_constellations()],
            )
        except Exception as exc:
            self._state = DeviceConnectionState.CONNECTED
            self._last_error = str(exc)
            raise

    async def save_to_flash(self) -> None:
        """Save the current config to device non-volatile memory.

        Raises:
            RuntimeError: If not connected or relay is running.
        """
        driver = self._require_connected()
        self._state = DeviceConnectionState.CONFIGURING
        try:
            await asyncio.to_thread(driver.save_to_flash)
            self._state = DeviceConnectionState.CONNECTED
            logger.info("Device configuration saved to flash")
        except Exception as exc:
            self._state = DeviceConnectionState.CONNECTED
            self._last_error = str(exc)
            raise

    # ------------------------------------------------------------------
    # Apply-config — the profile one-shot (issue #61)
    # ------------------------------------------------------------------

    async def apply_receiver_config(self, config: ReceiverConfig) -> ApplyConfigResult:
        """Apply a bare ``ReceiverConfig`` as an ordered series of layer=5 writes.

        Sequence: guards -> measurement rate -> ports -> constellations
        -> optimisations -> role fields (dyn_model, then tmode_mode) ->
        the full RTCM matrix -> read-back verify. Baud is out of scope
        here entirely — a submitted non-null ``ReceiverConfig.baud`` is
        rejected pre-write rather than silently ignored (see the follow-up
        ticket for baud support).

        Guards run before any write and refuse with nothing written:

        - **Baud out of scope.** A non-null ``baud`` with either UART
          value set is refused — applying it would mean reopening the
          serial link this very request is using, which is explicitly
          the follow-up ticket's job, not this one's.
        - **UBX-in liveness.** This console always manages the receiver
          over its own USB connection — UART1/UART2 are reserved for
          RTCM data-link output (``ReceiverConfig`` only allows those
          two as ``data_link_port``). So "the connected port" is
          always ``PortId.USB``; a submitted ``ports`` section that
          would turn UBX off on USB IN is refused, since it would cut
          the console's own control channel with nothing left to
          write it back with.
        - **Coordinate guard.** ``tmode_mode: fixed`` is refused unless
          the receiver already holds a valid, non-zero position —
          otherwise a fresh receiver becomes a base broadcasting 1005
          from ECEF/LLH 0,0,0.

        After the writes land, a non-blocking throughput estimate
        checks each ``data_link_port`` against its live baud rate, and
        a final read-back of the full RTCM matrix decides ``status``:
        a mismatch returns ``status="failed"`` with a per-cell diff
        (writes are left in flash, nothing is rolled back); a match
        returns ``status="ok"``.

        Raises:
            RuntimeError: If not connected or relay is running.
            ApplyConfigRefusedError: If a device-state guard rejects the
                config before any write.
        """
        driver = self._require_connected()

        if config.baud is not None and (
            config.baud.uart1 is not None or config.baud.uart2 is not None
        ):
            raise ApplyConfigRefusedError(
                "baud_out_of_scope",
                "baud is out of scope for apply-config — it would disturb "
                "the console's own link; leave it unset",
            )

        if config.ports is not None:
            usb_ports = config.ports.get(PortId.USB)
            if usb_ports is not None and UbxProtocol.UBX not in usb_ports.in_:
                raise ApplyConfigRefusedError(
                    "ubx_in_liveness",
                    "ports.USB.in must keep UBX enabled — the console manages "
                    "the receiver over its own USB connection",
                )

        if config.tmode_mode == TmodeMode.FIXED:
            current_base = await asyncio.to_thread(driver.get_base_config)
            if (
                current_base.latitude == 0.0
                and current_base.longitude == 0.0
                and current_base.altitude_m == 0.0
            ):
                raise ApplyConfigRefusedError(
                    "tmode_fixed_requires_coordinates",
                    "tmode_mode=fixed requires the receiver to already hold a "
                    "valid, non-zero position — survey-in or restore a saved "
                    "position first",
                )

        self._state = DeviceConnectionState.CONFIGURING
        try:
            await asyncio.to_thread(
                driver.configure_measurement_rate, config.meas_period_ms
            )

            if config.ports:
                in_map = {port: cfg.in_ for port, cfg in config.ports.items()}
                out_map = {port: cfg.out for port, cfg in config.ports.items()}
                await asyncio.to_thread(
                    driver.configure_port_protocols, in_map, out_map
                )

            if config.constellations is not None:
                current_gnss = await asyncio.to_thread(driver.get_gnss_config)
                wanted = set(config.constellations)
                updated_gnss = GnssConfig(
                    systems=[
                        system.model_copy(
                            update={"enabled": system.constellation in wanted}
                        )
                        for system in current_gnss.systems
                    ]
                )
                await asyncio.to_thread(driver.configure_gnss, updated_gnss)

            await asyncio.to_thread(
                driver.configure_optimisations,
                config.elevation_mask_deg,
                config.bds_b2_enabled,
                config.spi_enabled,
            )

            if config.dyn_model is not None:
                await asyncio.to_thread(driver.configure_dyn_model, config.dyn_model)
            if config.tmode_mode is not None:
                await asyncio.to_thread(driver.configure_tmode_mode, config.tmode_mode)

            await asyncio.to_thread(driver.apply_rtcm_matrix, config.rtcm_stream.matrix)

            self._state = DeviceConnectionState.CONNECTED
        except Exception as exc:
            self._state = DeviceConnectionState.CONNECTED
            self._last_error = str(exc)
            raise

        baud_rates = await asyncio.to_thread(driver.get_uart_baud_rates)
        warnings = _throughput_warnings(config, baud_rates)

        actual_rtcm = await asyncio.to_thread(driver.get_rtcm_port_config)
        diff = self._diff_rtcm_matrix(config.rtcm_stream.matrix, actual_rtcm)
        if diff:
            logger.warning(
                "apply-config read-back mismatch: %d cell(s) differ", len(diff)
            )
            return ApplyConfigResult(status="failed", diff=diff, warnings=warnings)

        logger.info("apply-config applied and read-back verified")
        return ApplyConfigResult(status="ok", warnings=warnings)

    @staticmethod
    def _diff_rtcm_matrix(
        expected: dict[RtcmRowId, dict[PortId, bool]],
        actual: RtcmPortConfig,
    ) -> list[ApplyConfigCellDiff]:
        """Diff the intended RTCM matrix against a live read-back.

        Covers all 36 cells (12 rows x UART1/UART2/USB) — the same
        scope ``apply_rtcm_matrix`` writes.
        """
        diffs: list[ApplyConfigCellDiff] = []
        for row in RtcmRowId:
            for port in (PortId.UART1, PortId.UART2, PortId.USB):
                expected_on = expected.get(row, {}).get(port, False)
                actual_on = actual.messages.get(row, {}).get(port.value, 0) > 0
                if expected_on != actual_on:
                    diffs.append(
                        ApplyConfigCellDiff(
                            row_id=row,
                            port=port,
                            expected=expected_on,
                            actual=actual_on,
                        )
                    )
        return diffs

    # ------------------------------------------------------------------
    # Status polling
    # ------------------------------------------------------------------

    async def get_position(self) -> GpsPosition:
        """Poll the current position solution from the receiver.

        Returns:
            Live position snapshot.

        Raises:
            RuntimeError: If not connected.
        """
        driver = self._require_connected()
        return await asyncio.to_thread(driver.get_position)

    async def get_survey_in_status(self) -> SurveyInProgress:
        """Poll the current survey-in progress.

        Returns:
            Survey-in progress snapshot.

        Raises:
            RuntimeError: If not connected.
        """
        driver = self._require_connected()
        return await asyncio.to_thread(driver.get_survey_in_status)

    def get_status(self) -> DeviceStatus:
        """Return a full device status snapshot.

        Returns:
            Device status including state, info, capabilities.
        """
        return DeviceStatus(
            state=self._state,
            port=self._port,
            baud_rate=self._baud_rate,
            info=self._info,
            capabilities=sorted(self.capabilities),
            survey_in=None,
            last_error=self._last_error,
            connected_at=self._connected_at,
        )


# ---------------------------------------------------------------------------
# Type alias for relay running check callback
# ---------------------------------------------------------------------------


class _RelayRunningCheck(Protocol):
    """Callable that returns whether the relay is currently running."""

    def __call__(self) -> bool: ...
