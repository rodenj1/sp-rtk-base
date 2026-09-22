"""Abstract base class for GPS receiver drivers.

Defines the vendor-neutral interface that all GPS receiver drivers
must implement. The DeviceService delegates to a concrete driver
without knowing the vendor-specific protocol (UBX, SBF, etc.).
"""

from __future__ import annotations

import abc
import contextlib
from collections.abc import Generator

from sp_rtk_base.models.device_models import (
    BaseMode,
    Candidate,
    CandidateVerdict,
    CurrentBaseConfig,
    DetectionOutcome,
    DetectionResult,
    DeviceCapability,
    DeviceInfo,
    DynModel,
    FixedBaseConfig,
    GnssConfig,
    GnssConstellation,
    GpsPosition,
    PortId,
    PortProtocolConfig,
    ReceiverScalarConfig,
    RtcmPortConfig,
    RtcmRowId,
    SerialPortInfo,
    SurveyInConfig,
    SurveyInProgress,
    UbxProtocol,
)

# ---------------------------------------------------------------------------
# Detection constants (issue #142)
# ---------------------------------------------------------------------------

#: Candidate rates in likelihood order. 115200 leads because it is what
#: the UI has always defaulted to; 57600 follows because it is what the
#: documented reference deployment actually runs at.
DETECTION_CANDIDATES: tuple[int, ...] = (
    115200,
    57600,
    38400,
    9600,
    230400,
    460800,
    921600,
    19200,
)

#: Wall-clock seconds per Candidate. Deliberately far below
#: ``UbloxDriver.CONNECT_TIMEOUT``: that 10s is sized for a real connect
#: to a receiver buried under RTCM traffic, and eight of them would be a
#: eighty-second spinner. A receiver that is going to answer answers in
#: tens of milliseconds.
DETECTION_BUDGET_S: float = 1.5


def detection_order(preferred_baud: int | None = None) -> tuple[int, ...]:
    """Candidate rates in the order a Detection should try them.

    *preferred_baud* leads when given — it is whatever the operator has
    selected — and is never tried twice. A preferred rate outside the
    standard set is still honoured: the operator may know something the
    likelihood order does not.
    """
    if preferred_baud is None:
        return DETECTION_CANDIDATES
    rest = tuple(r for r in DETECTION_CANDIDATES if r != preferred_baud)
    return (preferred_baud, *rest)


def _confirmation_rate(found: int) -> int:
    """A rate the receiver is deliberately *not* expected to answer at.

    A rate-sensitive UART runs at exactly one rate, so any other rate
    disconfirms. Picking from the standard set keeps it a rate the OS
    will actually open, and picking the far end of that set keeps the
    choice deterministic and obvious in a result.
    """
    slowest, fastest = min(DETECTION_CANDIDATES), max(DETECTION_CANDIDATES)
    return fastest if found == slowest else slowest


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
    # Detection — the baud-rate sweep (issue #142)
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def try_baud_candidate(
        self, port: str, baud_rate: int, budget_s: float
    ) -> tuple[CandidateVerdict, DeviceInfo | None]:
        """Open *port* at one rate and classify what came back.

        The only vendor-specific half of a Detection: what counts as an
        answer is a protocol question (MON-VER on u-blox), while the
        sweep around it is not. Implementations must leave the port
        closed on the way out, whatever the verdict.

        Args:
            port: Serial port path.
            baud_rate: The single Candidate rate to try.
            budget_s: Wall-clock seconds to spend before giving up.

        Returns:
            The Candidate's verdict, plus the identity read when — and
            only when — that verdict is ``ANSWERED``.
        """

    def is_detection_cancelled(self) -> bool:
        """Whether an in-flight Detection has been asked to stop.

        Concrete by default because a driver with no cancellation story
        is simply never cancelled. Drivers that already carry a cancel
        signal override this, which is what lets the existing Cancel
        button abort a sweep with no new plumbing.
        """
        return False

    @contextlib.contextmanager
    def detection_session(self) -> Generator[None]:
        """Wrap one whole sweep: set up before it, tear down after it.

        Exists because reusing the connect-cancel signal buys Detection a
        Cancel button for free but makes it inherit that signal's
        lifetime. ``cancel_connect`` raises the signal and only
        ``connect`` ever lowers it, so a driver whose last connect the
        operator *abandoned* still holds a raised cancel — and a
        Detection that trusted it would return having tried nothing.
        Drivers that carry such a signal lower it here, the way
        ``connect()`` already does on its own way in.

        The default does nothing, because a driver with no cancel signal
        and no port to release has nothing to do on either side.
        """
        yield

    def detect_baud(
        self,
        port: str,
        preferred_baud: int | None = None,
        budget_s: float = DETECTION_BUDGET_S,
    ) -> DetectionResult:
        """Sweep Candidate rates on *port* until the receiver answers.

        Vendor-neutral, and deliberately concrete: the ordering, the
        confirmation rule and the outcome classification are decisions
        that must not be re-made per driver. Only
        :meth:`try_baud_candidate` varies.

        *preferred_baud* is tried first — it is whatever the operator
        currently has selected, which is usually the remembered one and
        usually right. The rest follow in likelihood order.

        **Confirmation fires only on a first-Candidate hit.** A sweep
        that hit on a later Candidate has already watched earlier rates
        fail, so the port has proven itself rate-sensitive and there is
        nothing left to confirm. A hit on the very first Candidate
        carries no such disconfirming evidence, so one deliberately
        wrong rate is tried: if *that* answers too, the port ignores its
        baud setting entirely and the outcome is ``RATE_INDIFFERENT``.
        The confirming Candidate is recorded like any other, so the
        result stays auditable.

        Args:
            port: Serial port path to sweep.
            preferred_baud: Rate to try first, if any.
            budget_s: Wall-clock seconds allowed per Candidate.

        Returns:
            The :class:`DetectionResult` — never raises for a port that
            simply did not answer; that is ``NOT_FOUND``.
        """
        candidates: list[Candidate] = []
        opened_any = False
        first_open_error: Exception | None = None

        def attempt(rate: int) -> tuple[CandidateVerdict, DeviceInfo | None]:
            nonlocal opened_any, first_open_error
            try:
                verdict, info = self.try_baud_candidate(port, rate, budget_s)
                opened_any = True
            except Exception as exc:
                if first_open_error is None:
                    first_open_error = exc
                verdict, info = CandidateVerdict.SILENT, None
            candidates.append(Candidate(baud_rate=rate, verdict=verdict))
            return verdict, info

        with self.detection_session():
            for index, rate in enumerate(detection_order(preferred_baud)):
                if self.is_detection_cancelled():
                    break

                verdict, info = attempt(rate)
                if verdict is not CandidateVerdict.ANSWERED:
                    continue

                if index == 0 and not self.is_detection_cancelled():
                    disconfirming = _confirmation_rate(rate)
                    confirm_verdict, _ = attempt(disconfirming)
                    if confirm_verdict is CandidateVerdict.ANSWERED:
                        return DetectionResult(
                            outcome=DetectionOutcome.RATE_INDIFFERENT,
                            baud_rate=rate,
                            device=info,
                            candidates=candidates,
                        )

                return DetectionResult(
                    outcome=DetectionOutcome.FOUND,
                    baud_rate=rate,
                    device=info,
                    candidates=candidates,
                )

        # A port that never opened at *any* rate is not a Detection
        # outcome — it is a broken path the operator has to be told
        # about. Reporting "not found" for a permission error or a
        # missing device would send someone hunting a baud rate that
        # was never the problem, which is the opposite of the point.
        if not opened_any and first_open_error is not None:
            raise ConnectionError(
                f"Could not open {port} at any rate: {first_open_error}"
            ) from first_open_error

        return DetectionResult(
            outcome=DetectionOutcome.NOT_FOUND,
            candidates=candidates,
        )

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
    def get_dyn_model(self) -> DynModel:
        """Read the receiver's current dynamics platform model.

        Used by the survey-in pre-flight check (issue #63) to detect
        a receiver that hasn't had the base invariants applied.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the read fails.
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

        Used for the non-blocking apply-config throughput estimate,
        and as the "previous baud" to fall back to if a baud write's
        reopen fails (issue #62).

        Returns:
            Baud rate for UART1 and UART2 (USB has no baud rate).

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the read fails.
        """

    @abc.abstractmethod
    def configure_baud(self, uart1: int | None, uart2: int | None) -> None:
        """Write only the UART baud fields provided; ``None`` means leave untouched.

        Written last by the apply-config service, after every other
        key — a baud change on the port the console's own management
        link uses (UART1, per the documented FTDI deployment) must
        not disturb the ACK of any earlier write still in flight
        (issue #62). USB has no baud rate and isn't a parameter here.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the write fails.
        """

    @abc.abstractmethod
    def drain_warnings(self) -> list[str]:
        """Drain and return every warning queued since the last drain (issue #99).

        The apply-config service calls this after each write step that
        actually runs, tagging whatever comes back with that step's
        name. The channel means exactly one thing: the write appears
        to have succeeded, but something the sync check is
        structurally unable to see is wrong — e.g. a value that landed
        in RAM but not flash, or a constellation the firmware
        acknowledged but does not appear to act on. No severity field.
        A concrete driver with nothing to report simply returns an
        empty list every time — the u-blox driver's only producer is
        its write-and-verify helper's flash read-back (issue #103).

        Returns:
            Warning messages queued since the last call; empty if none.

        Raises:
            ConnectionError: If not connected.
        """

    @abc.abstractmethod
    def get_receiver_scalars(self) -> ReceiverScalarConfig:
        """Batched read of every scalar CFG value the full receiver read needs.

        Baud (UART1/UART2), measurement rate, dynamics model, TMODE
        mode, elevation mask, BeiDou B2 and SPI enablement — one poll
        instead of up to seven separate getters (issue #97).

        Returns:
            The current scalar configuration.

        Raises:
            ConnectionError: If not connected.
            RuntimeError: If the read fails.
        """

    @abc.abstractmethod
    def reconnect_at_baud(self, baud_rate: int) -> DeviceInfo:
        """Reopen the serial connection on the same port at a new baud rate.

        Called immediately after a ``configure_baud`` write changes
        UART1's baud — the port this console's own management link
        uses — so the read-back verify that follows has a live link
        to run against (issue #62).

        Returns:
            Refreshed device identity from the reconnected receiver.

        Raises:
            RuntimeError: If the driver was never connected (no saved
                port to reopen).
            ConnectionError: If the device doesn't respond at the
                given baud.
            TimeoutError: If the device doesn't respond in time.
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
    def configure_gnss(self, constellations: set[GnssConstellation]) -> None:
        """Assertive durable write of the per-constellation enable keys (issue #104).

        Every constellation the vendor-neutral :class:`GnssConstellation`
        enum knows about is written explicitly — on for the members of
        ``constellations``, off for every other member — at whatever
        layer the concrete driver considers durable. Channel allocation
        is left entirely to the firmware; this call has no channel
        parameter. IMES and NavIC are never touched — they have no
        member in :class:`GnssConstellation`, so there is no key for
        this call to write for them.

        Args:
            constellations: Exactly the constellations that should end
                up enabled.

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
