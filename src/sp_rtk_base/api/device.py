"""Device API — GPS receiver connection and configuration endpoints.

Provides REST endpoints for managing the GPS receiver connection,
querying device info, configuring base-station mode, and polling
survey-in progress.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sp_rtk_base.models.api_models import DeviceActionResponse, DeviceConnectRequest
from sp_rtk_base.models.config_models import (
    BaseStationPosition,
    DeviceProfile,
    InputProfile,
)
from sp_rtk_base.models.device_models import (
    CurrentBaseConfig,
    DeviceStatus,
    FixedBaseConfig,
    GnssConfig,
    GpsPosition,
    RtcmMessageConfig,
    SerialPortInfo,
    SurveyInConfig,
    SurveyInProgress,
)
from sp_rtk_base.services import (
    get_config_service,
    get_device_service,
    get_relay_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers import create_driver
from sp_rtk_base.services.relay_service import RelayService

router = APIRouter(prefix="/api/device", tags=["device"])


# ---------------------------------------------------------------------------
# Serial port discovery
# ---------------------------------------------------------------------------


@router.get("/ports", response_model=list[SerialPortInfo])
async def list_serial_ports() -> list[SerialPortInfo]:
    """List available serial ports with GPS device detection."""
    from sp_rtk_base.services.drivers.base import GpsReceiverDriver

    return GpsReceiverDriver.list_serial_ports()


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


@router.post("/connect", response_model=DeviceActionResponse)
async def connect_device(
    request: DeviceConnectRequest,
    svc: DeviceService = Depends(get_device_service),
) -> DeviceActionResponse:
    """Connect to a GPS receiver.

    Loads the driver for the specified vendor, then connects on the
    given serial port.  Returns 409 if already connected or relay
    is running.
    """
    try:
        driver = create_driver(request.vendor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        svc.set_driver(driver)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        info = await svc.connect(request.port, request.baud_rate)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ConnectionError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return DeviceActionResponse(
        status="ok",
        message=f"Connected to {info.vendor} {info.model} on {request.port}",
    )


@router.post("/disconnect", response_model=DeviceActionResponse)
async def disconnect_device(
    svc: DeviceService = Depends(get_device_service),
) -> DeviceActionResponse:
    """Disconnect from the GPS receiver."""
    if not svc.is_available:
        raise HTTPException(status_code=409, detail="No device connected")

    await svc.disconnect()
    return DeviceActionResponse(status="ok", message="Device disconnected")


# ---------------------------------------------------------------------------
# Status & capabilities
# ---------------------------------------------------------------------------


@router.get("/status", response_model=DeviceStatus)
async def get_device_status(
    svc: DeviceService = Depends(get_device_service),
) -> DeviceStatus:
    """Return the full device status snapshot."""
    return svc.get_status()


@router.get("/capabilities", response_model=list[str])
async def get_device_capabilities(
    svc: DeviceService = Depends(get_device_service),
) -> list[str]:
    """Return capability list of the loaded driver.

    Returns an empty list if no driver is loaded.
    """
    return sorted(c.value for c in svc.capabilities)


# ---------------------------------------------------------------------------
# Base station configuration
# ---------------------------------------------------------------------------


@router.post("/configure/survey-in", response_model=DeviceActionResponse)
async def configure_survey_in(
    config: SurveyInConfig,
    svc: DeviceService = Depends(get_device_service),
) -> DeviceActionResponse:
    """Configure the receiver for survey-in mode."""
    try:
        await svc.configure_survey_in(config)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeviceActionResponse(
        status="ok",
        message=f"Survey-in configured: {config.min_duration_seconds}s, {config.accuracy_limit_mm}mm",
    )


@router.post("/cancel-survey-in", response_model=DeviceActionResponse)
async def cancel_survey_in(
    svc: DeviceService = Depends(get_device_service),
) -> DeviceActionResponse:
    """Cancel an in-progress survey-in by disabling TMODE.

    Sends ``CFG_TMODE_MODE=0`` to the receiver.  Safe to call even
    when no survey is running (the receiver stays in disabled mode).

    Returns 409 if not connected or the relay is running.
    """
    try:
        await svc.cancel_survey_in()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeviceActionResponse(status="ok", message="Survey-in cancelled")


@router.post("/configure/fixed-base", response_model=DeviceActionResponse)
async def configure_fixed_base(
    config: FixedBaseConfig,
    svc: DeviceService = Depends(get_device_service),
) -> DeviceActionResponse:
    """Configure the receiver for fixed-position base mode."""
    try:
        await svc.configure_fixed_base(config)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeviceActionResponse(
        status="ok",
        message=f"Fixed base configured: {config.latitude:.6f}, {config.longitude:.6f}",
    )


@router.post("/configure/rtcm", response_model=DeviceActionResponse)
async def configure_rtcm_messages(
    config: RtcmMessageConfig,
    svc: DeviceService = Depends(get_device_service),
) -> DeviceActionResponse:
    """Configure RTCM message output on the receiver."""
    try:
        await svc.configure_rtcm_messages(config)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeviceActionResponse(
        status="ok",
        message=f"RTCM messages configured: {config.message_ids}",
    )


@router.post("/save", response_model=DeviceActionResponse)
async def save_to_flash(
    svc: DeviceService = Depends(get_device_service),
) -> DeviceActionResponse:
    """Save the current device configuration to flash memory."""
    try:
        await svc.save_to_flash()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeviceActionResponse(status="ok", message="Configuration saved to flash")


# ---------------------------------------------------------------------------
# GNSS constellation configuration
# ---------------------------------------------------------------------------


@router.get("/gnss", response_model=GnssConfig)
async def get_gnss_config(
    svc: DeviceService = Depends(get_device_service),
) -> GnssConfig:
    """Read the current GNSS constellation configuration from the receiver.

    Returns which constellations (GPS, GLONASS, Galileo, BeiDou, SBAS, QZSS)
    are enabled and their channel allocation.
    """
    try:
        return await svc.get_gnss_config()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/gnss", response_model=DeviceActionResponse)
async def configure_gnss(
    config: GnssConfig,
    svc: DeviceService = Depends(get_device_service),
) -> DeviceActionResponse:
    """Apply GNSS constellation configuration to the receiver.

    Enable/disable satellite systems and configure tracking channel allocation.
    """
    try:
        await svc.configure_gnss(config)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    enabled = config.enabled_constellations()
    return DeviceActionResponse(
        status="ok",
        message=f"GNSS configured: {[c.value for c in enabled]}",
    )


# ---------------------------------------------------------------------------
# Current base station config (read from device)
# ---------------------------------------------------------------------------


@router.get("/base-config", response_model=CurrentBaseConfig)
async def get_base_config(
    svc: DeviceService = Depends(get_device_service),
) -> CurrentBaseConfig:
    """Read the current base station configuration from the receiver.

    Returns the active TMODE mode (disabled/survey_in/fixed) and,
    for fixed mode, the configured coordinates.
    """
    try:
        return await svc.get_base_config()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Live position & survey-in polling
# ---------------------------------------------------------------------------


@router.get("/position", response_model=GpsPosition)
async def get_position(
    svc: DeviceService = Depends(get_device_service),
) -> GpsPosition:
    """Poll the current position solution from the receiver.

    Returns the latest NAV-PVT data including fix type, coordinates,
    accuracy, satellite count, and RTK status.
    """
    try:
        return await svc.get_position()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/survey-in", response_model=SurveyInProgress)
async def get_survey_in_progress(
    svc: DeviceService = Depends(get_device_service),
) -> SurveyInProgress:
    """Poll the current survey-in progress from the receiver."""
    try:
        return await svc.get_survey_in_status()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Diagnostic — UBX-CFG-RST experimentation
#
# Exposed to find the canonical reset variant that clears the
# HPG 1.12 NAV-SVIN.dur accumulator (a BBR-backed counter that the
# CFG_TMODE_MODE=0 layer=7 VALSET and the resetMode=0x09+pos CFG-RST
# both leave intact).  Not part of the normal operational surface —
# kept under /debug/ to make that obvious.
# ---------------------------------------------------------------------------

# Allowed resetMode values.
# - 1, 2, 8, 9 are software resets — USB stays connected.
# - 0 (hardware reset immediate) and 4 (hardware reset after delay)
#   cause a USB re-enumeration and require ``read_after_state=False``
#   in the request, otherwise the after-state read hangs on the
#   disconnected serial port.  The endpoint enforces that pairing.
_ALLOWED_RESET_MODES = {0, 1, 2, 4, 8, 9}
_HARDWARE_RESET_MODES = {0, 4}

# pyubx2's named bitfield keys on CFG-RST.navBbrMask.  Anything else
# would either silently fail or raise inside pyubx2's payload builder.
_ALLOWED_BBR_BITS = {
    "eph",
    "alm",
    "health",
    "klob",
    "pos",
    "clkd",
    "osc",
    "utc",
    "rtc",
    "aop",
}


class CfgRstRequest(BaseModel):
    """Body for ``POST /api/device/debug/cfg-rst``."""

    reset_mode: int = Field(
        ...,
        description=(
            "UBX CFG-RST resetMode byte. Allowed: "
            "0=hardware reset immediate, 1=controlled SW reset, "
            "2=controlled GNSS-only reset, 4=hardware reset after delay, "
            "8=controlled GNSS stop, 9=controlled GNSS start. "
            "Hardware resets (0x00 / 0x04) require "
            "``read_after_state=false`` since they drop the USB."
        ),
    )
    bbr_bits: list[str] = Field(
        default_factory=list,
        description=(
            "Named navBbrMask bits to set. Valid: eph, alm, health, "
            "klob, pos, clkd, osc, utc, rtc, aop. An empty list means "
            "navBbrMask=0 (no BBR sections cleared)."
        ),
    )
    wait_seconds: float = Field(
        3.0,
        ge=0.0,
        le=30.0,
        description=(
            "Sleep between the write and the after-state read. "
            "Ignored when read_after_state=false."
        ),
    )
    read_after_state: bool = Field(
        True,
        description=(
            "When false, skip the post-write sleep and the NAV-SVIN "
            "after-poll.  Required for hardware resets "
            "(reset_mode 0 or 4) because the USB re-enumerates and "
            "the after-poll would hang on a stale serial handle.  "
            "Operator must reconnect the device via "
            "/api/device/connect after the response returns."
        ),
    )


class CfgRstResponse(BaseModel):
    """Diagnostic snapshot of the CFG-RST round trip."""

    before: SurveyInProgress
    after: SurveyInProgress | None = Field(
        None,
        description=(
            "Post-write NAV-SVIN read, or null when "
            "read_after_state=false (e.g. after a hardware reset)."
        ),
    )
    wait_seconds: float
    ubx_sent_hex: str = Field(
        ..., description="Hex of the serialised UBX frame actually written."
    )


@router.post("/debug/cfg-rst", response_model=CfgRstResponse)
async def debug_cfg_rst(
    body: CfgRstRequest,
    svc: DeviceService = Depends(get_device_service),
) -> CfgRstResponse:
    """Send an arbitrary UBX-CFG-RST and report before/after NAV-SVIN.

    Diagnostic-only: used to discover which ``resetMode`` + BBR-bit
    combination actually clears the NAV-SVIN ``dur`` accumulator on
    a given ZED-F9P firmware revision.
    """
    if body.reset_mode not in _ALLOWED_RESET_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"reset_mode={body.reset_mode} not allowed. "
                f"Allowed: {sorted(_ALLOWED_RESET_MODES)}."
            ),
        )

    if body.reset_mode in _HARDWARE_RESET_MODES and body.read_after_state:
        raise HTTPException(
            status_code=400,
            detail=(
                f"reset_mode={body.reset_mode} is a hardware reset that "
                "drops the USB serial connection.  The after-state read "
                "would hang on a stale handle.  Set "
                "read_after_state=false and reconnect via "
                "/api/device/connect after this request returns."
            ),
        )

    invalid = [b for b in body.bbr_bits if b not in _ALLOWED_BBR_BITS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown bbr_bits: {invalid}. Allowed: {sorted(_ALLOWED_BBR_BITS)}."
            ),
        )

    bbr_kwargs = {bit: 1 for bit in body.bbr_bits}

    try:
        before, after, wire_bytes = await svc.send_cfg_rst_diagnostic(
            body.reset_mode,
            body.wait_seconds,
            bbr_kwargs,
            body.read_after_state,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return CfgRstResponse(
        before=before,
        after=after,
        wait_seconds=body.wait_seconds if body.read_after_state else 0.0,
        ubx_sent_hex=wire_bytes.hex(),
    )


# ---------------------------------------------------------------------------
# Handoff — device → relay
# ---------------------------------------------------------------------------


@router.post("/handoff", response_model=DeviceActionResponse)
async def handoff_to_relay(
    svc: DeviceService = Depends(get_device_service),
    relay: RelayService = Depends(get_relay_service),
    cfg: ConfigService = Depends(get_config_service),
) -> DeviceActionResponse:
    """Disconnect device and start relay using same serial port.

    1. Remembers port/baud from the active device connection.
    2. Disconnects the GPS receiver driver.
    3. Updates the input config to ``usb_serial`` with the same port/baud.
    4. Persists the device profile and input config.
    5. Starts the relay engine.

    Returns 409 if the device is not connected or the relay is already running.
    """
    if not svc.is_connected:
        raise HTTPException(status_code=409, detail="Device not connected")

    if relay.is_running:
        raise HTTPException(status_code=409, detail="Relay is already running")

    status = svc.get_status()
    port = status.port or ""
    baud = status.baud_rate or 115200

    # 1. Persist device profile
    cfg.save_device_profile(
        DeviceProfile(
            vendor=svc.driver.vendor_name if svc.driver else "ublox",
            port=port,
            baud_rate=baud,
        )
    )

    # 2. Disconnect driver (releases serial port)
    await svc.disconnect()

    # 3. Configure relay input source with same serial port
    input_profile = InputProfile(
        source="usb_serial",
        config={"port": port, "baudrate": baud},
    )
    cfg.save_input_config(input_profile)

    # 4. Build relay configs and start
    relay_input = input_profile.to_relay_config()
    relay_dests = [d.to_relay_config() for d in cfg.get_destinations() if d.enabled]

    try:
        await relay.start_relay(relay_input, relay_dests)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Relay start failed: {exc}"
        ) from exc

    return DeviceActionResponse(
        status="ok",
        message=f"Handed off {port} to relay engine",
    )


# ---------------------------------------------------------------------------
# Survey-in auto-promote → fixed base
# ---------------------------------------------------------------------------


@router.post("/promote-survey-in", response_model=DeviceActionResponse)
async def promote_survey_in(
    svc: DeviceService = Depends(get_device_service),
) -> DeviceActionResponse:
    """Promote a completed survey-in to permanent fixed-base mode.

    Reads the surveyed position from the receiver, switches the receiver
    to fixed-base mode with those exact coordinates, and saves the
    configuration to flash.  This means the receiver will start in
    fixed-base mode on next power-up (no re-survey needed).

    Returns 409 if the survey is not complete (``valid=False``).
    """
    try:
        status = await svc.get_survey_in_status()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not status.valid:
        raise HTTPException(
            status_code=409,
            detail="Survey-in has not completed (valid=False)",
        )

    if status.latitude is None or status.longitude is None or status.altitude_m is None:
        raise HTTPException(
            status_code=500,
            detail="Survey-in is valid but position data is missing",
        )

    # Switch receiver to fixed-base mode with the surveyed position
    fixed_cfg = FixedBaseConfig(
        latitude=status.latitude,
        longitude=status.longitude,
        altitude_m=status.altitude_m,
        accuracy_mm=max(1, int(status.mean_accuracy_mm)),
    )
    try:
        await svc.configure_fixed_base(fixed_cfg)
        await svc.save_to_flash()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeviceActionResponse(
        status="ok",
        message=(
            f"Promoted to fixed base: "
            f"{status.latitude:.7f}, {status.longitude:.7f}, "
            f"{status.altitude_m:.3f}m (±{status.mean_accuracy_mm:.0f}mm)"
        ),
    )


# ---------------------------------------------------------------------------
# Base station position profiles
# ---------------------------------------------------------------------------


@router.get("/base-positions", response_model=list[BaseStationPosition])
async def list_base_positions(
    cfg: ConfigService = Depends(get_config_service),
) -> list[BaseStationPosition]:
    """List all saved base station position profiles."""
    return cfg.get_base_positions()


@router.post("/base-positions", response_model=DeviceActionResponse, status_code=201)
async def save_base_position(
    position: BaseStationPosition,
    cfg: ConfigService = Depends(get_config_service),
) -> DeviceActionResponse:
    """Save a named base station position profile.

    If a profile with the same name already exists, it is replaced.
    """
    cfg.save_base_position(position)
    return DeviceActionResponse(
        status="ok",
        message=f"Position '{position.name}' saved",
    )


@router.delete("/base-positions/{name}", response_model=DeviceActionResponse)
async def delete_base_position(
    name: str,
    cfg: ConfigService = Depends(get_config_service),
) -> DeviceActionResponse:
    """Delete a saved base station position by name."""
    if not cfg.delete_base_position(name):
        raise HTTPException(status_code=404, detail=f"Position '{name}' not found")
    return DeviceActionResponse(status="ok", message=f"Position '{name}' deleted")


@router.post("/base-positions/{name}/restore", response_model=DeviceActionResponse)
async def restore_base_position(
    name: str,
    svc: DeviceService = Depends(get_device_service),
    cfg: ConfigService = Depends(get_config_service),
) -> DeviceActionResponse:
    """Restore a saved position to the receiver as fixed-base mode.

    Reads the named position from config, configures the receiver in
    fixed-base mode with those coordinates, and saves to flash.
    """
    position = cfg.get_base_position(name)
    if position is None:
        raise HTTPException(status_code=404, detail=f"Position '{name}' not found")

    fixed_cfg = FixedBaseConfig(
        latitude=position.latitude,
        longitude=position.longitude,
        altitude_m=position.altitude_m,
        accuracy_mm=max(1, int(position.accuracy_mm)),
    )
    try:
        await svc.configure_fixed_base(fixed_cfg)
        await svc.save_to_flash()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeviceActionResponse(
        status="ok",
        message=(
            f"Restored '{name}': {position.latitude:.7f}, "
            f"{position.longitude:.7f}, {position.altitude_m:.3f}m"
        ),
    )
