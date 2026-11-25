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
    UNNAMED_MODULE,
    BluetoothLink,
    ConnectStage,
    ConnectStageCode,
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
class ModuleLine:
    """The Bluetooth side's module line, and the MAC shown small under it."""

    text: str
    detail: str | None


def mac_detail(mac: str | None) -> str | None:
    """The MAC as the small detail or hover beside a Module name."""
    return f"MAC {mac}" if mac else None


@dataclass(frozen=True)
class StageRow:
    """One row of the Stage list: the Stage, where it stands, and why."""

    label: str
    status: ConnectStageStatus
    detail: str | None


def connected_line(status: DeviceStatus) -> str:
    """The status line for a connected Console link.

    A Bluetooth link names its module by Module name;
    :func:`connected_line_detail` gives the MAC for the hover.
    """
    link = status.link
    if isinstance(link, BluetoothLink):
        port = status.console_port.value if status.console_port else "unknown"
        return f"Connected: Bluetooth · {link.module_name} · console port {port}"
    if isinstance(link, SerialLink):
        return f"Connected: {link.port} @ {link.baud_rate}"
    return "Connected"


def connected_line_detail(status: DeviceStatus) -> str | None:
    """The hover on the connected status line: a Bluetooth link's MAC."""
    link = status.link
    if isinstance(link, BluetoothLink):
        return mac_detail(link.mac)
    return None


def bluetooth_module_line(
    profile: InputProfile | None, stages: list[ConnectStageResult] | None
) -> ModuleLine | None:
    """The Input profile's module, as the Bluetooth side shows it.

    ``None`` when the Input profile isn't Bluetooth: the side shows
    :data:`SET_UP_BLUETOOTH_PROMPT` instead. Whether the module is paired
    can't be read from BlueZ cheaply (ADR 0001: no public ``Paired``
    accessor), so the line says what the last connect found, and says so:
    a Bond can be lost since (a Force-repair, an eviction).

    The line leads with the Module name (or :data:`UNNAMED_MODULE`); the
    MAC is the small detail under it.
    """
    if profile is None or profile.source != "bluetooth":
        return None
    name = str(profile.config.get("device_name") or UNNAMED_MODULE)
    mac = str(profile.config.get("mac_address") or "") or None
    return ModuleLine(
        text=f"{name} · {_pairing(stages)}", detail=mac_detail(mac) or "no MAC"
    )


def _pairing(stages: list[ConnectStageResult] | None) -> str:
    """What the last connect's Pair Stage says about the Bond.

    Passed or skipped: there was one. A refused PIN: there wasn't, since
    the PIN is only tried without a Bond. Any other failure (the module
    wasn't found, the adapter couldn't be used) says nothing about it.
    """
    pair = next((s for s in stages or [] if s.stage is ConnectStage.PAIR), None)
    if pair is None or pair.status in (
        ConnectStageStatus.PENDING,
        ConnectStageStatus.RUNNING,
    ):
        return "pairing not checked yet"
    if pair.status in (ConnectStageStatus.PASSED, ConnectStageStatus.SKIPPED):
        return "paired at the last connect"
    if pair.code is ConnectStageCode.PIN_REJECTED:
        return "not paired at the last connect"
    return "pairing unknown"


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
