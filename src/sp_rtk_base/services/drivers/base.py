"""Abstract base class for GPS receiver drivers.

Defines the vendor-neutral interface that all GPS receiver drivers
must implement. The DeviceService delegates to a concrete driver
without knowing the vendor-specific protocol (UBX, SBF, etc.).
"""

from __future__ import annotations

import abc

from sp_rtk_base.models.device_models import (
    BaseMode,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceInfo,
    DynModel,
    FixedBaseConfig,
    GnssConfig,
    GpsPosition,
    PortId,
    PortProtocolConfig,
    RtcmMessageConfig,
    RtcmPortConfig,
    RtcmRowId,
    SerialPortInfo,
    SurveyInConfig,
    SurveyInProgress,
    UbxProtocol,
)


class GpsReceiverDriver(abc.ABC):
    """Abstract GPS receiver driver interface.

    Concrete implementations handle vendor-specific communication
    (e.g. u-blox UBX protocol, Septentrio SBF, etc.).

    All I/O methods are synchronous — the DeviceService wraps them
    with ``asyncio.to_thread()`` for async API integration.
    """

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def vendor_name(self) -> str:
        """Human-readable vendor name (e.g. ``"u-blox"``)."""

    @abc.abstractmethod
    def get_capabilities(self) -> set[DeviceCapability]:
        """Return the set of capabilities this driver supports.

        Returns:
            Set of :class:`DeviceCapability` values.
        """

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def connect(self, port: str, baud_rate: int = 115200) -> DeviceInfo:
        """Open a serial connection and identify the device.

        Must read device identity information (model, firmware, etc.)
        during connection.

        Args:
            port: Serial port path (e.g. ``/dev/ttyACM0``).
            baud_rate: Serial baud rate.

        Returns:
            Device identity information.

        Raises:
            ConnectionError: If the connection fails.
            TimeoutError: If the device does not respond.
        """

    @abc.abstractmethod
    def disconnect(self) -> None:
        """Close the serial connection.

        Safe to call when already disconnected.
        """

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Whether the driver currently has an open connection."""

    # ------------------------------------------------------------------
    # Base station configuration
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def configure_survey_in(self, config: SurveyInConfig) -> None:
        """Configure the receiver for survey-in base station mode.

        Args:
            config: Survey-in parameters.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If configuration fails (NAK, timeout, etc.).
        """

    @abc.abstractmethod
    def configure_fixed_base(self, config: FixedBaseConfig) -> None:
        """Configure the receiver for fixed-position base station mode.

        Args:
            config: Fixed base parameters.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If configuration fails.
        """

    @abc.abstractmethod
    def disable_base_mode(self) -> None:
        """Disable base station mode (TMODE → disabled).

        Used to abort an in-progress survey-in or to clear a fixed-base
        configuration.  Equivalent to ``CFG_TMODE_MODE=0`` on u-blox.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If configuration fails.
        """

    @abc.abstractmethod
    def configure_rtcm_messages(self, config: RtcmMessageConfig) -> None:
        """Enable/disable RTCM message outputs on the receiver.

        Args:
            config: RTCM message selection and rate.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If configuration fails.
        """

    @abc.abstractmethod
    def get_rtcm_config(self) -> RtcmMessageConfig:
        """Read the current RTCM message output configuration.

        Returns:
            Current RTCM message selection and rate.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the read fails.
        """

    @abc.abstractmethod
    def get_rtcm_port_config(self) -> RtcmPortConfig:
        """Read RTCM output config for all ports (USB, UART1, etc.).

        Returns:
            Per-message, per-port rate configuration.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the read fails.
        """

    @abc.abstractmethod
    def configure_rtcm_ports(self, config: RtcmPortConfig) -> None:
        """Apply multi-port RTCM output configuration.

        Args:
            config: Per-message, per-port rate configuration.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If configuration fails.
        """

    @abc.abstractmethod
    def get_port_protocols(self) -> PortProtocolConfig:
        """Read live in/out protocol state for UART1, UART2 and USB.

        Returns:
            Per-port enabled input/output protocols.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the read fails.
        """

    @abc.abstractmethod
    def save_to_flash(self) -> None:
        """Save the current configuration to non-volatile memory.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If save fails.
        """

    # ------------------------------------------------------------------
    # Apply-config primitives (issue #61)
    #
    # Lower-level writers the apply-config service orchestrates in a
    # fixed order. Each is independently callable and independently
    # verified — none of them know about ``ReceiverConfig``, keeping
    # the profile schema out of the vendor-neutral driver layer.
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def configure_port_protocols(
        self,
        in_protocols: dict[PortId, list[UbxProtocol]],
        out_protocols: dict[PortId, list[UbxProtocol]],
    ) -> None:
        """Assertive per-port in/out protocol write for the ports given.

        Only ports present in either mapping are touched — a port with
        no entry in either dict keeps its current state untouched.

        Args:
            in_protocols: port -> exactly the input protocols that
                should be enabled (every other protocol on that port
                is turned off).
            out_protocols: same, for output protocols.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the write fails.
        """

    @abc.abstractmethod
    def configure_measurement_rate(self, period_ms: int) -> None:
        """Write the measurement period in milliseconds.

        Args:
            period_ms: Raw ``CFG_RATE_MEAS`` period in ms (not Hz).
                ``CFG_RATE_NAV`` is pinned to 1 alongside it.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the write fails.
        """

    @abc.abstractmethod
    def configure_dyn_model(self, model: DynModel) -> None:
        """Write the receiver's dynamics platform model.

        Args:
            model: Desired dynamics class.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the write fails.
        """

    @abc.abstractmethod
    def configure_tmode_mode(self, mode: BaseMode) -> None:
        """Write ``CFG_TMODE_MODE`` directly, without touching position keys.

        A plain mode assertion, not a full base-mode transition —
        callers that need survey-in/fixed-base position writes should
        use ``configure_survey_in``/``configure_fixed_base`` instead.
        Any coordinate precondition for ``fixed`` mode is the caller's
        responsibility.

        Args:
            mode: Desired TMODE mode.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the write fails.
        """

    @abc.abstractmethod
    def configure_optimisations(
        self,
        elevation_mask_deg: int | None,
        bds_b2_enabled: bool | None,
        spi_enabled: bool | None,
    ) -> None:
        """Write only the optimisation fields provided; ``None`` means leave untouched.

        Args:
            elevation_mask_deg: Minimum satellite elevation, degrees.
            bds_b2_enabled: Whether the BeiDou B2 signal is enabled.
            spi_enabled: Whether the SPI interface is enabled.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the write fails.
        """

    @abc.abstractmethod
    def apply_rtcm_matrix(self, matrix: dict[RtcmRowId, dict[PortId, bool]]) -> None:
        """Assertive write of all 36 cells (12 rows x UART1/UART2/USB).

        Every cell is written explicitly, including zeros, so a row
        enabled by a previous profile/session that isn't in ``matrix``
        ends up off rather than surviving as a silent superset.

        Args:
            matrix: row id -> {port: enabled}. A missing cell means off.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the write fails.
        """

    @abc.abstractmethod
    def get_uart_baud_rates(self) -> dict[PortId, int]:
        """Read the live UART1/UART2 baud rates.

        Used only for the non-blocking apply-config throughput
        estimate — baud itself is never written by apply-config.

        Returns:
            Baud rate for UART1 and UART2 (USB has no baud rate).

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the read fails.
        """

    # ------------------------------------------------------------------
    # GNSS constellation configuration
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def get_gnss_config(self) -> GnssConfig:
        """Read the current GNSS constellation configuration.

        Returns:
            Current GNSS system configuration.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the read fails.
        """

    @abc.abstractmethod
    def configure_gnss(self, config: GnssConfig) -> None:
        """Write GNSS constellation configuration to the receiver.

        Args:
            config: Desired GNSS system configuration.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If configuration fails (NAK, timeout, etc.).
        """

    # ------------------------------------------------------------------
    # Status polling
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def get_position(self) -> GpsPosition:
        """Poll the current position solution from the receiver.

        Reads the latest NAV-PVT (or vendor equivalent) message and
        returns a vendor-neutral position snapshot.

        Returns:
            Current position solution.

        Raises:
            ConnectionError: If not connected.
        """

    @abc.abstractmethod
    def get_survey_in_status(self) -> SurveyInProgress:
        """Poll the current survey-in progress.

        Returns:
            Survey-in progress snapshot.

        Raises:
            ConnectionError: If not connected.
        """

    @abc.abstractmethod
    def get_device_info(self) -> DeviceInfo:
        """Re-read device identity information.

        Returns:
            Current device info.

        Raises:
            ConnectionError: If not connected.
        """

    @abc.abstractmethod
    def get_base_config(self) -> CurrentBaseConfig:
        """Read the current base station configuration from the receiver.

        Returns the active TMODE mode (disabled / survey-in / fixed)
        and, for fixed mode, the configured coordinates.

        Returns:
            Current base station configuration.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the read fails.
        """

    # ------------------------------------------------------------------
    # Port discovery (static — works without a connection)
    # ------------------------------------------------------------------

    @staticmethod
    def list_serial_ports() -> list[SerialPortInfo]:
        """Discover available serial ports on the system.

        Uses ``serial.tools.list_ports`` to enumerate ports and
        flags likely GPS receivers based on known USB vendor IDs.

        Returns:
            List of available serial ports.
        """
        from serial.tools import list_ports  # type: ignore[import-untyped]

        # Known GPS receiver USB vendor IDs
        gps_vendor_ids: set[int] = {
            0x1546,  # u-blox
            0x067B,  # Prolific (common USB-to-serial for GPS)
            0x10C4,  # Silicon Labs (CP210x, common for GPS)
            0x0403,  # FTDI (common for GPS)
        }

        ports: list[SerialPortInfo] = []
        for p in list_ports.comports():
            ports.append(
                SerialPortInfo(
                    port=p.device,
                    description=p.description or "",
                    manufacturer=p.manufacturer or "",
                    vid=p.vid,
                    pid=p.pid,
                    serial_number=p.serial_number or "",
                    is_gps=p.vid is not None and p.vid in gps_vendor_ids,
                )
            )
        # GPS-likely ports first, then alphabetical by port name
        ports.sort(key=lambda p: (not p.is_gps, p.port))
        return ports
