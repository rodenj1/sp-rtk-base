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
from typing import NamedTuple, Protocol

from sp_rtk_base.models.device_models import (
    ALL_RTCM_MESSAGE_IDS,
    BaseInvariantsCheck,
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
    ReceiverScalarConfig,
    RtcmOutputPort,
    RtcmPortConfig,
    RtcmRowId,
    SurveyInConfig,
    SurveyInProgress,
    UbxProtocol,
)
from sp_rtk_base.models.device_models import (
    BaseMode as TmodeMode,
)
from sp_rtk_base.models.profile_models import (
    APPLY_STEPS,
    MATRIX_PORTS,
    ApplyConfigResult,
    ApplyStepResult,
    ApplyStepWarning,
    BaudAssertion,
    PortProtocolSet,
    ReceiverApplyRequest,
    ReceiverAssertion,
    RtcmStreamConfig,
    diff_receiver_assertions,
    merge_profile_into_assertion,
    step_for_diff_path,
)
from sp_rtk_base.profiles import BUILTIN_PROFILES
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


class ApplyConfigLinkLostError(Exception):
    """The receiver was reconfigured but its own management link couldn't be reopened.

    Raised by ``apply_receiver_config`` (issue #62) when a UART1 baud
    write lands but reopening the console's own connection at the new
    baud fails, and a single retry at the previous baud also can't
    restore contact. The flash write stands — nothing is rolled back,
    since writing the old baud back would fight the write that just
    landed, and retrying the old rate forever would just hang. Carries
    both bauds so the caller can name a concrete recovery step: power-
    cycle the receiver, or reconnect manually at ``new_baud``.
    """

    def __init__(self, previous_baud: int, new_baud: int) -> None:
        self.previous_baud = previous_baud
        self.new_baud = new_baud
        super().__init__(
            "Receiver reconfigured but link lost — could not reopen the "
            f"console's connection at the new baud ({new_baud}) or the "
            f"previous baud ({previous_baud}). Power-cycle the receiver "
            "or reconnect manually at the new baud rate."
        )


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

# The built-in profile whose ``ports``/``dyn_model``/``rtcm_stream``
# values define "the base invariants" for the survey-in pre-flight
# check (issue #63) and its one-click remedy. There is exactly one
# built-in today (see test_builtin_profiles.py's
# test_imports_cleanly_with_exactly_one_builtin) and it is the base
# station reference profile.
_BASE_INVARIANTS_PROFILE_NAME = "ublox-f9p-base-standard"


def _throughput_warnings(
    assertion: ReceiverAssertion,
    data_link_port: list[PortId],
    baud_rates: dict[PortId, int],
) -> list[str]:
    """Non-blocking advisories when estimated RTCM output nears port capacity.

    Takes the whole ``ReceiverAssertion`` plus *data_link_port*
    separately — ``data_link_port`` has no CFG key and isn't part of
    the assertion (issue #98), but the check still needs it alongside
    ``rtcm_stream.matrix`` and ``meas_period_ms``.
    """
    matrix = assertion.rtcm_stream.matrix
    hz = 1000.0 / assertion.meas_period_ms
    warnings: list[str] = []
    for port in data_link_port:
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


def build_receiver_assertion(
    rtcm: RtcmPortConfig,
    ports: PortProtocolConfig,
    gnss: GnssConfig,
    scalars: ReceiverScalarConfig,
) -> ReceiverAssertion:
    """Compose a full live receiver read into one ``ReceiverAssertion``.

    Pure — reshapes the driver's four already-fetched reads (RTCM
    matrix, port protocols, GNSS constellations, the batched scalar
    poll) into the schema the Advanced GPS page seeds ``form``/``live``
    from (issue #97). No device I/O of its own.
    """
    matrix = {
        row_id: {
            port: rtcm.is_enabled(row_id, RtcmOutputPort(port.value))
            for port in MATRIX_PORTS
        }
        for row_id in ALL_RTCM_MESSAGE_IDS
    }
    port_set = {
        port: PortProtocolSet(
            **{"in": ports.enabled_in(port), "out": ports.enabled_out(port)}
        )
        for port in PortId
    }
    return ReceiverAssertion(
        baud=BaudAssertion(uart1=scalars.uart1_baud, uart2=scalars.uart2_baud),
        meas_period_ms=scalars.meas_period_ms,
        constellations=gnss.enabled_constellations(),
        ports=port_set,
        dyn_model=scalars.dyn_model,
        tmode_mode=scalars.tmode_mode,
        elevation_mask_deg=scalars.elevation_mask_deg,
        bds_b2_enabled=scalars.bds_b2_enabled,
        spi_enabled=scalars.spi_enabled,
        rtcm_stream=RtcmStreamConfig(matrix=matrix),
    )


class ReceiverAssertionRead(NamedTuple):
    """One ``get_receiver_assertion()`` read: the sync-set assertion, the
    raw multi-port RTCM read-back the page's I2C/SPI advisory needs, and
    the raw GNSS config the constellation apply-step needs (issue #99).

    I2C/SPI aren't part of ``assertion.rtcm_stream``'s matrix — that
    mirrors ``ReceiverConfig``'s UART1/UART2/USB-only scope — so the
    advisory display needs the wider read ``rtcm`` already carries.
    Likewise, ``assertion.constellations`` is just the flat enabled set —
    writing a constellation change needs each system's channel tuning,
    which only the raw ``gnss`` read carries. Bundling all three here
    means one receiver read serves every caller, rather than any of them
    re-polling RTCM or GNSS a second time.
    """

    assertion: ReceiverAssertion
    rtcm: RtcmPortConfig
    gnss: GnssConfig


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
        # Steps whose drained warnings were non-empty on the previous
        # apply-config call — excluded from the next call's skip so
        # pressing Apply again actually retries them (issue #99).
        # Reset on every fresh connect: a new session starts clean.
        self._steps_warned_last_apply: set[str] = set()

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
            self._steps_warned_last_apply = set()
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
            # Drain immediately: this call sits outside apply_receiver_
            # config's per-step loop, the only other drain site. Left
            # undrained, a flash-divergence warning (issue #103) would
            # sit on the driver's shared queue and get misattributed to
            # whatever unrelated step a later Apply happens to run.
            for warning in await asyncio.to_thread(driver.drain_warnings):
                logger.warning("configure_survey_in: %s", warning)
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

    async def check_base_invariants(self) -> BaseInvariantsCheck:
        """Non-blocking pre-flight check for the base invariants (issue #63).

        Compares the live receiver against the two invariants that used
        to be force-applied on every base-mode transition (issues #38
        and #40, retired in #63): stationary dynamics, and at least one
        RTCM row enabled on every data-link port of the built-in base
        profile (:data:`_BASE_INVARIANTS_PROFILE_NAME`). Never raises
        for a mismatch — callers (the Survey-In confirmation dialog)
        show the warnings but let Start proceed regardless; use
        :meth:`apply_base_invariants` for the one-click remedy.

        Raises:
            RuntimeError: If not connected or relay is running.
        """
        driver = self._require_connected()
        profile = BUILTIN_PROFILES[_BASE_INVARIANTS_PROFILE_NAME]

        live_dyn_model = await asyncio.to_thread(driver.get_dyn_model)
        live_rtcm = await asyncio.to_thread(driver.get_rtcm_port_config)

        warnings: list[str] = []
        if profile.dyn_model is not None and live_dyn_model != profile.dyn_model:
            warnings.append(
                f"Dynamics model is {live_dyn_model.value}, not "
                f"{profile.dyn_model.value} — a base station should run "
                "stationary dynamics."
            )

        for port in profile.data_link_port:
            has_rtcm = any(
                rates.get(port.value, 0) > 0 for rates in live_rtcm.messages.values()
            )
            if not has_rtcm:
                warnings.append(
                    f"No RTCM messages are enabled on {port.value} — this "
                    "data-link port would broadcast no corrections."
                )

        return BaseInvariantsCheck(warnings=warnings)

    async def apply_base_invariants(self) -> ApplyConfigResult:
        """Apply the built-in base profile as the one-click remedy for issue #63.

        Reuses :meth:`apply_receiver_config` — the built-in profile
        (:data:`_BASE_INVARIANTS_PROFILE_NAME`) already specifies the
        port protocols, dynamics model and RTCM matrix that satisfy
        :meth:`check_base_invariants`. The profile is merged onto a
        fresh live read via :func:`merge_profile_into_assertion` (issue
        #98 — ``apply_receiver_config`` now takes a whole
        ``ReceiverApplyRequest`` rather than a bare, partially-optional
        config), with ``baud`` stripped from the profile copy first: the
        two invariants this remedy exists for (dynamics model,
        RTCM-on-data-link-port) never touch baud, so a one-click warning
        fix should not risk stranding the console's own link on a UART1
        baud change the operator didn't ask for — stripping it means the
        merge falls back to the live baud, which then compares equal in
        ``apply_receiver_config``'s unchanged-baud skip.

        Raises:
            RuntimeError: If not connected or relay is running.
            ApplyConfigRefusedError: If a device-state guard rejects the
                profile before any write.
        """
        profile = BUILTIN_PROFILES[_BASE_INVARIANTS_PROFILE_NAME].model_copy(
            update={"baud": None}
        )
        read = await self.get_receiver_assertion()
        merged = merge_profile_into_assertion(profile, read.assertion)
        request = ReceiverApplyRequest(
            assertion=merged, data_link_port=list(profile.data_link_port)
        )
        return await self.apply_receiver_config(request)

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
            # Drain immediately — see the matching comment in
            # configure_survey_in (issue #103): this call sits outside
            # apply_receiver_config's per-step loop, the only other
            # drain site, so an undrained warning would otherwise be
            # misattributed to a later, unrelated Apply step.
            for warning in await asyncio.to_thread(driver.drain_warnings):
                logger.warning("configure_rtcm_ports: %s", warning)
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

    async def get_receiver_assertion(self) -> ReceiverAssertionRead:
        """Read the whole receiver in one go as a ``ReceiverAssertion``.

        Four driver reads — RTCM matrix, port protocols, GNSS
        constellations, and the batched scalar poll (baud, meas rate,
        dyn model, tmode mode, elevation mask, BeiDou B2, SPI) — landing
        as three CFG-VALGET polls rather than seven separate round
        trips, composed by the pure :func:`build_receiver_assertion`
        (issue #97). Every field the Advanced GPS page seeds ``form``/
        ``live`` from reflects the receiver's actual value.

        Returns the raw RTCM read alongside the assertion — the page's
        I2C/SPI advisory needs it (ports the matrix doesn't claim), and
        re-polling it separately would reintroduce exactly the extra
        lock-acquire/RX-drain/read-loop round trip this method exists
        to collapse away.

        Raises:
            RuntimeError: If not connected.
        """
        driver = self._require_connected()
        return await self._read_full_assertion(driver)

    async def _read_full_assertion(
        self, driver: GpsReceiverDriver
    ) -> ReceiverAssertionRead:
        """The four driver reads behind :meth:`get_receiver_assertion`.

        Shared with ``apply_receiver_config``'s pre-write pre-read
        (issue #99) so there's exactly one place composing a full
        receiver read, rather than the pre-read duplicating this
        method's body to also get at the raw ``gnss`` config it needs.
        """
        rtcm = await asyncio.to_thread(driver.get_rtcm_port_config)
        ports = await asyncio.to_thread(driver.get_port_protocols)
        gnss = await asyncio.to_thread(driver.get_gnss_config)
        scalars = await asyncio.to_thread(driver.get_receiver_scalars)
        assertion = build_receiver_assertion(rtcm, ports, gnss, scalars)
        return ReceiverAssertionRead(assertion=assertion, rtcm=rtcm, gnss=gnss)

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

    async def apply_receiver_config(
        self, request: ReceiverApplyRequest
    ) -> ApplyConfigResult:
        """Apply the form as an ordered series of per-step, layer=5 writes (issue #99).

        *request* is the envelope Apply sends: a fully-populated
        ``ReceiverAssertion`` (every receiver field, no "leave alone"
        omissions) plus ``data_link_port`` — the port selection has no
        CFG key of its own and is never written, only used by the
        guards/warnings below and by ``ReceiverApplyRequest``'s own
        cross-field validators.

        Sequence: guards -> a fresh pre-read -> :data:`APPLY_STEPS` in
        order (measurement rate, ports, constellations, optimisations,
        dyn_model, tmode_mode, the RTCM matrix, baud) -> reopen (if
        UART1's baud changed) -> read-back verify. Baud is last, after
        every other key has landed and only once every other write's
        ACK is safely in — a baud change is the one write that can move
        the console's own management link out from under this very
        request (issue #62).

        **The pre-read decides the no-op.** One fresh full read, taken
        at the instant of Apply (never the page's seed — a stale seed
        another client or a reboot has invalidated can't be trusted to
        rest a "nothing to do" claim on), is diffed per-leaf against
        *request.assertion* and grouped by :func:`profile_models.
        step_for_diff_path`. A step is skipped when every leaf it
        covers already matches; a step that runs writes its complete
        key set assertively (never a partial, per-leaf write) — so an
        unqualified whole-form assert doesn't push every key to flash
        on every Apply, but pressing Apply on a genuinely unchanged
        form still performs no writes at all and just re-verifies.
        **Exception:** a step that produced a warning on the *previous*
        Apply is excluded from this skip, so it actually retries
        instead of the remedy being a no-op that quietly erases its own
        warning.

        **Step outcomes.** Each step reports ``"ok"``, ``"failed"`` or
        ``"skipped"`` in ``result.steps``, in execution order. A step
        that raises stops every subsequent step (they report
        ``"skipped"`` too — nothing further is written) but never
        propagates: only a pre-write refusal or a lost link still
        raises out of this method. The read-back always runs
        regardless, so ``result.diff``/``result.read_back`` state what
        the receiver actually holds even when a step failed.

        **Warnings.** After each step that runs, ``GpsReceiverDriver.
        drain_warnings`` is drained and whatever comes back is tagged
        with that step's name onto ``result.step_warnings`` — see
        :class:`profile_models.ApplyStepWarning`. A step that ran and
        drained nothing clears its membership in the warned set; one
        that ran and drained something (again) keeps it. A step this
        Apply never got the chance to run — skipped because an earlier
        step failed — carries its prior membership forward unchanged,
        so an unresolved warning survives an unrelated failure rather
        than being silently dropped the moment one Apply doesn't reach
        it.

        Guards run before any write and refuse with nothing written:

        - **UBX-in liveness.** This console always manages the receiver
          over its own USB connection — UART1/UART2 are reserved for
          RTCM data-link output. So "the connected port" is always
          ``PortId.USB``; a submitted ``ports`` section that would turn
          UBX off on USB IN is refused, since it would cut the
          console's own control channel with nothing left to write it
          back with.
        - **Survey-in active.** Whole-form assert makes ``tmode_mode``
          a required assertion field, so an Apply that leaves it alone
          would otherwise silently cancel an in-progress survey-in that
          may be hours in. Refused only when this Apply would actually
          change base mode (``request.assertion.tmode_mode`` differs
          from the pre-read) while a survey-in is running — an Apply
          that leaves base mode alone still proceeds mid-survey.
        - **Coordinate guard.** ``tmode_mode: fixed`` is refused unless
          the receiver already holds a valid, non-zero position —
          otherwise a fresh receiver becomes a base broadcasting 1005
          from ECEF/LLH 0,0,0.

        ``request.assertion.baud.uart1`` is treated as the port the
        console's own management link is on, per the documented
        deployment (FTDI -> UART1 at 57600, see
        docs/zed-f9p-base-station-config-reference.md) — a separate,
        narrower premise from the UBX-in guard's "always USB" one
        above; the two guard different concerns (which named port must
        keep UBX in, vs. which physical serial link this process
        itself has open) and issue #62 doesn't ask for them to be
        reconciled. When UART1's baud actually changes, this reopens
        the connection at the new baud once the write lands, before
        anything else runs. Reopening is deterministic — the baud was
        just written — so one attempt normally suffices; a failure
        retries once at the previous baud purely to leave the caller
        with *some* link back, then raises ``ApplyConfigLinkLostError``
        regardless of that retry's outcome: the flash write stands
        either way (rolling it back would fight the write that just
        landed, and retrying the old rate forever would hang). A
        changed UART2 baud never triggers a reopen — it isn't the port
        this console is on.

        After the writes land, a non-blocking throughput estimate
        checks each ``data_link_port`` against its live baud rate, and
        a fresh full read-back (:meth:`get_receiver_assertion`) is
        diffed per-leaf against *request.assertion*
        (:func:`profile_models.diff_receiver_assertions`) to decide
        ``status``: any mismatch returns ``status="failed"`` with the
        per-leaf, path-keyed diff (writes are left in flash, nothing is
        rolled back); an empty diff returns ``status="ok"``. The
        read-back is always attached as ``read_back``, whichever
        ``status`` — the page's ``live`` state syncs from it either way.

        Raises:
            RuntimeError: If not connected or relay is running.
            ApplyConfigRefusedError: If a device-state guard rejects the
                request before any write.
            ApplyConfigLinkLostError: If a UART1 baud write lands but
                reopening the console's own link fails at both the new
                and the previous baud.
        """
        driver = self._require_connected()
        assertion = request.assertion

        usb_ports = assertion.ports.get(PortId.USB)
        if usb_ports is not None and UbxProtocol.UBX not in usb_ports.in_:
            raise ApplyConfigRefusedError(
                "ubx_in_liveness",
                "ports.USB.in must keep UBX enabled — the console manages "
                "the receiver over its own USB connection",
            )

        pre_read = await self._read_full_assertion(driver)
        pre_assertion = pre_read.assertion

        if assertion.tmode_mode != pre_assertion.tmode_mode:
            survey = await asyncio.to_thread(driver.get_survey_in_status)
            if survey.active:
                raise ApplyConfigRefusedError(
                    "survey_in_active",
                    "a survey-in is running — changing base mode would "
                    "cancel it. Cancel the survey from the Survey page "
                    "first, or leave tmode_mode unchanged to apply "
                    "everything else",
                )

        if assertion.tmode_mode == TmodeMode.FIXED:
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

        pre_diff = diff_receiver_assertions(assertion, pre_assertion)
        changed_steps = {step_for_diff_path(d.path) for d in pre_diff}

        self._state = DeviceConnectionState.CONFIGURING
        steps: list[ApplyStepResult] = []
        step_warnings: list[ApplyStepWarning] = []
        # Starts as a copy of the previous Apply's warned steps and is
        # only touched by a step that actually gets to run this time —
        # a step skipped by an earlier failure (``blocked``) hasn't been
        # given the chance to resolve its warning, so its membership
        # carries forward untouched rather than being dropped just
        # because this particular Apply didn't reach it.
        updated_warned_steps = set(self._steps_warned_last_apply)
        new_uart1: int | None = None
        blocked = False

        for step_name in APPLY_STEPS:
            if blocked:
                steps.append(ApplyStepResult(step=step_name, status="skipped"))
                continue

            if (
                step_name not in changed_steps
                and step_name not in self._steps_warned_last_apply
            ):
                steps.append(ApplyStepResult(step=step_name, status="skipped"))
                continue

            try:
                await self._run_apply_step(driver, step_name, assertion, pre_read.gnss)
            except Exception as exc:
                logger.warning("apply-config step %r failed: %s", step_name, exc)
                self._last_error = f"apply-config step {step_name!r} failed: {exc}"
                steps.append(ApplyStepResult(step=step_name, status="failed"))
                blocked = True
                continue

            if step_name == "baud" and assertion.baud.uart1 != pre_assertion.baud.uart1:
                new_uart1 = assertion.baud.uart1

            drained = await asyncio.to_thread(driver.drain_warnings)
            if drained:
                updated_warned_steps.add(step_name)
                step_warnings.extend(
                    ApplyStepWarning(step=step_name, message=message)
                    for message in drained
                )
            else:
                updated_warned_steps.discard(step_name)
            steps.append(ApplyStepResult(step=step_name, status="ok"))

        self._state = DeviceConnectionState.CONNECTED
        self._steps_warned_last_apply = updated_warned_steps

        if new_uart1 is not None:
            await self._reopen_after_baud_write(driver, new_uart1)

        baud_rates = await asyncio.to_thread(driver.get_uart_baud_rates)
        warnings = _throughput_warnings(assertion, request.data_link_port, baud_rates)

        read = await self.get_receiver_assertion()
        diff = diff_receiver_assertions(assertion, read.assertion)
        if diff:
            logger.warning(
                "apply-config read-back mismatch: %d leaf(ves) differ", len(diff)
            )
            return ApplyConfigResult(
                status="failed",
                read_back=read.assertion,
                diff=diff,
                warnings=warnings,
                steps=steps,
                step_warnings=step_warnings,
            )

        logger.info("apply-config applied and read-back verified")
        return ApplyConfigResult(
            status="ok",
            read_back=read.assertion,
            warnings=warnings,
            steps=steps,
            step_warnings=step_warnings,
        )

    async def _run_apply_step(
        self,
        driver: GpsReceiverDriver,
        step_name: str,
        assertion: ReceiverAssertion,
        pre_gnss: GnssConfig,
    ) -> None:
        """Perform one write step's driver call (issue #99).

        *pre_gnss* is the raw pre-read GNSS config — the constellation
        step needs it (not just ``assertion.constellations``) to
        preserve each system's channel tuning, which
        ``ReceiverAssertion`` doesn't carry.
        """
        if step_name == "meas_period_ms":
            await asyncio.to_thread(
                driver.configure_measurement_rate, assertion.meas_period_ms
            )
        elif step_name == "ports":
            in_map = {port: cfg.in_ for port, cfg in assertion.ports.items()}
            out_map = {port: cfg.out for port, cfg in assertion.ports.items()}
            await asyncio.to_thread(driver.configure_port_protocols, in_map, out_map)
        elif step_name == "constellations":
            wanted = set(assertion.constellations)
            updated_gnss = GnssConfig(
                systems=[
                    system.model_copy(
                        update={"enabled": system.constellation in wanted}
                    )
                    for system in pre_gnss.systems
                ]
            )
            await asyncio.to_thread(driver.configure_gnss, updated_gnss)
        elif step_name == "optimisations":
            await asyncio.to_thread(
                driver.configure_optimisations,
                assertion.elevation_mask_deg,
                assertion.bds_b2_enabled,
                assertion.spi_enabled,
            )
        elif step_name == "dyn_model":
            await asyncio.to_thread(driver.configure_dyn_model, assertion.dyn_model)
        elif step_name == "tmode_mode":
            await asyncio.to_thread(driver.configure_tmode_mode, assertion.tmode_mode)
        elif step_name == "rtcm_matrix":
            await asyncio.to_thread(
                driver.apply_rtcm_matrix, assertion.rtcm_stream.matrix
            )
        elif step_name == "baud":
            await asyncio.to_thread(
                driver.configure_baud, assertion.baud.uart1, assertion.baud.uart2
            )
        else:  # pragma: no cover
            raise AssertionError(f"unhandled apply step: {step_name!r}")

    async def _reopen_after_baud_write(
        self, driver: GpsReceiverDriver, new_baud: int
    ) -> None:
        """Reopen the console's own link after a UART1 baud write (issue #62).

        Reopening is deterministic — the new baud was just written —
        so a single attempt normally suffices. A failure retries once
        at the previous baud purely to leave the caller with *some*
        link back if possible, then raises ``ApplyConfigLinkLostError``
        regardless of whether that retry itself succeeded: the flash
        write stands either way, since writing the old baud back would
        fight the write that just landed, and retrying the old rate
        forever would hang.
        """
        previous_baud = self._baud_rate
        assert (
            previous_baud is not None
        )  # set by connect(), which _require_connected() guarantees ran

        try:
            info = await asyncio.to_thread(driver.reconnect_at_baud, new_baud)
            self._baud_rate = new_baud
            self._info = info
            return
        except Exception as exc:
            logger.warning(
                "apply-config: reopen at new baud %d failed (%s), retrying "
                "once at previous baud %d",
                new_baud,
                exc,
                previous_baud,
            )

        try:
            info = await asyncio.to_thread(driver.reconnect_at_baud, previous_baud)
            self._baud_rate = previous_baud
            self._info = info
            self._state = DeviceConnectionState.CONNECTED
        except Exception as exc:
            logger.warning(
                "apply-config: retry at previous baud %d also failed (%s)",
                previous_baud,
                exc,
            )
            self._state = DeviceConnectionState.DISCONNECTED

        self._last_error = (
            f"Receiver reconfigured but link lost — could not reopen at "
            f"the new baud ({new_baud}) or the previous baud ({previous_baud})"
        )
        logger.error(self._last_error)
        raise ApplyConfigLinkLostError(previous_baud, new_baud)

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
