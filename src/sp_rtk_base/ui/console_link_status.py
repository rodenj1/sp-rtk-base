"""What the Connect panel shows about a Console link (rtk_development#43).

Both Connection cards (Survey and GPS config) render these, so they say
the same thing; and ``ui/pages/*`` is excluded from the coverage gate, so
the wording is decided here, in a covered module, as ``detection_status``
does for a Detection.

Operator copy says "paired" rather than "Bond", for the reason given in
``bluetooth_status``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sp_rtk_base.models.config_models import InputProfile
from sp_rtk_base.models.device_models import (
    BluetoothLink,
    ConnectStage,
    ConnectStageResult,
    ConnectStageStatus,
    DeviceConnectionState,
    DeviceStatus,
    SerialLink,
)

#: Shown on the Bluetooth side, with Connect disabled, when the Input
#: profile isn't Bluetooth: the console's Bluetooth link has no settings
#: of its own.
SET_UP_BLUETOOTH_PROMPT = (
    "Bluetooth uses the Bluetooth Input profile's module, and there isn't "
    "one. Set one up on the Input page."
)

#: Under the module line: where its settings live.
MODULE_NOTE = "From the Bluetooth Input profile. Change it on the Input page."

_STAGE_LABEL: dict[ConnectStage, str] = {
    ConnectStage.PAIR: "Pair",
    ConnectStage.CONNECT: "Connect",
    ConnectStage.IDENTIFY: "Identify",
}


@dataclass(frozen=True)
class StageRow:
    """One row of the Stage list: the Stage, where it stands, and why."""

    label: str
    status: ConnectStageStatus
    detail: str | None


def connected_line(status: DeviceStatus) -> str:
    """The status line for a connected Console link."""
    link = status.link
    if isinstance(link, BluetoothLink):
        name = link.device_name or link.mac or "module"
        port = status.console_port.value if status.console_port else "unknown"
        return f"Connected: Bluetooth · {name} · console port {port}"
    if isinstance(link, SerialLink):
        return f"Connected: {link.port} @ {link.baud_rate}"
    return "Connected"


def bluetooth_module_line(
    profile: InputProfile | None, stages: list[ConnectStageResult] | None
) -> str | None:
    """The Input profile's module, as the Bluetooth side shows it.

    ``None`` when the Input profile isn't Bluetooth: the side shows
    :data:`SET_UP_BLUETOOTH_PROMPT` instead. Whether the module is paired
    can't be read from BlueZ (ADR 0001), so it is what the last connect
    found: its Pair Stage passed or was skipped (already paired), or
    failed.
    """
    if profile is None or profile.source != "bluetooth":
        return None
    name = str(profile.config.get("device_name") or "Unnamed module")
    mac = str(profile.config.get("mac_address") or "no MAC")
    return f"{name} · {mac} · {_pairing(stages)}"


def _pairing(stages: list[ConnectStageResult] | None) -> str:
    pair = next((s.status for s in stages or [] if s.stage is ConnectStage.PAIR), None)
    if pair in (ConnectStageStatus.PASSED, ConnectStageStatus.SKIPPED):
        return "Paired"
    if pair is ConnectStageStatus.FAILED:
        return "not paired"
    return "pairing not checked yet"


def stage_rows(stages: list[ConnectStageResult] | None) -> list[StageRow]:
    """The Stage list of the last Bluetooth connect, in order.

    A failure carries the layer's message and that Stage's advice; a
    skipped Pair says it was already paired.
    """
    rows: list[StageRow] = []
    for s in stages or []:
        detail = s.message
        if s.status is ConnectStageStatus.SKIPPED and s.stage is ConnectStage.PAIR:
            detail = "Already paired"
        elif s.status is ConnectStageStatus.FAILED and s.advice:
            detail = f"{s.message}. {s.advice}" if s.message else s.advice
        rows.append(StageRow(_STAGE_LABEL[s.stage], s.status, detail))
    return rows


def kind_locked(state: DeviceConnectionState) -> bool:
    """The Serial / Bluetooth toggle can't change under a live or opening link."""
    return state in (DeviceConnectionState.CONNECTING, DeviceConnectionState.CONNECTED)


def cancel_allowed(stages: list[ConnectStageResult] | None) -> bool:
    """Cancel can't interrupt Bluetooth discovery, so not during Connect."""
    return not any(
        s.stage is ConnectStage.CONNECT and s.status is ConnectStageStatus.RUNNING
        for s in stages or []
    )
