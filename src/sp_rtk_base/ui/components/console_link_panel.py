"""The Connect panel's Console link chooser, shared by both Connection cards.

Variant A of the prototype (rtk_development#35, built in #43): a status
line, a **Serial cable / Bluetooth** toggle that locks while connecting or
connected, and under it either the serial fields (Serial Port, Baud Rate,
Detect) or a read-only line naming the Bluetooth Input profile's module.
The Driver select serves both kinds. During a Bluetooth connect a Stage
list shows Pair, Connect and Identify as they pass.

The Survey and GPS config pages each build one, so the two cards behave
the same. The page keeps its own Connect, Disconnect and Cancel buttons and
what connecting does to the rest of the page; it binds the buttons here so
the panel can enable them. What the panel says is decided in
``ui/console_link_status`` (a covered module).
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

from collections.abc import Awaitable
from typing import TypeVar

from nicegui import ui

from sp_rtk_base.models.config_models import DeviceProfile
from sp_rtk_base.models.device_models import (
    BAUD_RATES,
    DEFAULT_BAUD,
    BluetoothLink,
    ConnectStageResult,
    ConnectStageStatus,
    ConsoleLink,
    ConsoleLinkKind,
    DeviceConnectionState,
    SerialLink,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers import list_drivers
from sp_rtk_base.ui.console_link_status import (
    MODULE_NOTE,
    SET_UP_BLUETOOTH_PROMPT,
    bluetooth_module_line,
    cancel_allowed,
    connected_line,
    kind_locked,
    stage_rows,
)

_T = TypeVar("_T")

_KINDS: dict[str, str] = {"serial": "Serial cable", "bluetooth": "Bluetooth"}

_STAGE_ICON: dict[ConnectStageStatus, tuple[str, str]] = {
    ConnectStageStatus.PENDING: ("radio_button_unchecked", "grey"),
    ConnectStageStatus.RUNNING: ("hourglass_top", "info"),
    ConnectStageStatus.PASSED: ("check_circle", "positive"),
    ConnectStageStatus.FAILED: ("cancel", "negative"),
    ConnectStageStatus.SKIPPED: ("remove_circle_outline", "grey"),
}

#: How often the Stage list is refreshed while a Bluetooth connect runs (s).
_STAGE_POLL_S = 0.3


class ConsoleLinkPanel:
    """Status line, kind toggle, and the fields of the chosen kind.

    Building it lays out the status line, the toggle and the fields row in
    the current container. :meth:`add_stage_list` lays out the Stage list
    wherever the page wants it (under its buttons).
    """

    def __init__(self, svc: DeviceService, config_svc: ConfigService) -> None:
        self._svc = svc
        self._config_svc = config_svc
        self._kind: ConsoleLinkKind = "serial"
        self._connect_btn: ui.button | None = None
        self._cancel_btn: ui.button | None = None
        self._refresh_btn: ui.button | None = None
        self._stage_list: ui.column | None = None

        self._status_row = ui.row().classes("items-center gap-2 q-mt-sm")
        self.toggle = (
            ui.toggle(_KINDS, value=self._kind, on_change=self._on_kind_change)
            .props("no-caps data-testid=console-link-kind")
            .classes("q-mt-sm")
        )
        with ui.row().classes("w-full gap-4 q-mt-sm sp-metric-row"):
            # A typed path is kept even when it isn't listed.
            self.port_select = ui.select(
                options=[],
                label="Serial Port",
                with_input=True,
                new_value_mode="add-unique",
            ).classes("col-grow")
            self.baud_select = ui.select(
                options={r: str(r) for r in BAUD_RATES},
                label="Baud Rate",
                value=DEFAULT_BAUD,
            ).classes("w-40")
            self.detect_btn = (
                ui.button("Detect", icon="search")
                .props("outline color=info")
                .classes("self-center sp-detect-baud")
                .tooltip(
                    "Try each baud rate on the selected port until the receiver answers"
                )
            )
            self._bluetooth_side = ui.column().classes("col-grow gap-0 self-center")
            self.driver_select = ui.select(
                options=list_drivers(), label="Driver", value="ublox"
            ).classes("w-40")

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def bind_buttons(
        self,
        connect_btn: ui.button,
        cancel_btn: ui.button,
        refresh_btn: ui.button,
    ) -> None:
        """The page's buttons the panel enables or hides by kind."""
        self._connect_btn = connect_btn
        self._cancel_btn = cancel_btn
        self._refresh_btn = refresh_btn

    def add_stage_list(self) -> None:
        """Lay out the Stage list here."""
        self._stage_list = (
            ui.column()
            .classes("gap-0 q-mt-xs")
            .props("data-testid=console-link-stages")
        )

    # ------------------------------------------------------------------
    # The chosen link
    # ------------------------------------------------------------------

    @property
    def kind(self) -> ConsoleLinkKind:
        return self._kind

    def link(self) -> ConsoleLink | None:
        """The Console link to connect over, or ``None`` (with a notice)."""
        if self._kind == "bluetooth":
            return BluetoothLink()
        port = self.port_select.value
        if not port:
            ui.notify("Select a serial port", type="warning")
            return None
        return SerialLink(
            port=str(port), baud_rate=int(self.baud_select.value or DEFAULT_BAUD)
        )

    @property
    def vendor(self) -> str:
        return str(self.driver_select.value or "ublox")

    async def watch_connect(self, connecting: Awaitable[_T]) -> _T:
        """Await a connect, refreshing the Stage list while it runs."""
        timer = ui.timer(_STAGE_POLL_S, self.update)
        try:
            return await connecting
        finally:
            timer.cancel()
            self.update()

    # ------------------------------------------------------------------
    # The saved device profile
    # ------------------------------------------------------------------

    def load_saved(self) -> None:
        """Pre-fill from the saved device profile, on the last kind used."""
        profile = self._config_svc.get_device_profile()
        if profile is None:
            return
        if profile.port:
            self.port_select.value = profile.port
        if profile.baud_rate:
            self.baud_select.value = profile.baud_rate
        if profile.vendor:
            self.driver_select.value = profile.vendor
        self._kind = profile.kind
        self.toggle.value = profile.kind

    def save(self) -> None:
        """Remember the kind, serial fields and driver for next time."""
        saved = self._config_svc.get_device_profile()
        try:
            self._config_svc.save_device_profile(
                DeviceProfile(
                    vendor=self.vendor,
                    kind=self._kind,
                    port=str(self.port_select.value or (saved.port if saved else "")),
                    baud_rate=int(self.baud_select.value or DEFAULT_BAUD),
                )
            )
        except Exception:
            pass  # Non-critical

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _on_kind_change(self) -> None:
        self._kind = "bluetooth" if self.toggle.value == "bluetooth" else "serial"
        self.update()

    def update(self) -> None:
        """Bring the panel up to date with the device service."""
        status = self._svc.get_status()
        state = status.state
        bluetooth = self._kind == "bluetooth"

        self._render_status(state, connected_line(status))
        self.toggle.set_enabled(not kind_locked(state))
        for serial_only in (self.port_select, self.baud_select, self.detect_btn):
            serial_only.set_visibility(not bluetooth)
        if self._refresh_btn is not None:
            self._refresh_btn.set_visibility(not bluetooth)
        self._bluetooth_side.set_visibility(bluetooth)

        module = bluetooth_module_line(
            self._config_svc.get_input_config(), status.connect_stages
        )
        self._render_bluetooth_side(module)

        stages = status.connect_stages if bluetooth else None
        if self._connect_btn is not None:
            self._connect_btn.set_enabled(not bluetooth or module is not None)
        if self._cancel_btn is not None:
            self._cancel_btn.set_enabled(cancel_allowed(stages))
        self._render_stages(stages)

    def _render_status(self, state: DeviceConnectionState, connected: str) -> None:
        self._status_row.clear()
        with self._status_row:
            if state == DeviceConnectionState.CONNECTED:
                ui.icon("check_circle").classes("text-positive text-h6")
                text, tone = connected, "text-positive"
            elif state == DeviceConnectionState.CONNECTING:
                ui.spinner(size="sm")
                text, tone = "Connecting...", "text-warning"
            elif state == DeviceConnectionState.ERROR:
                ui.icon("error").classes("text-negative text-h6")
                text, tone = "Error", "text-negative"
            else:
                ui.icon("link_off").classes("text-grey text-h6")
                text, tone = "Disconnected", "text-grey"
            ui.label(text).classes(tone).props("data-testid=console-link-status")

    def _render_bluetooth_side(self, module: str | None) -> None:
        self._bluetooth_side.clear()
        with self._bluetooth_side:
            if module is None:
                ui.label(SET_UP_BLUETOOTH_PROMPT).classes(
                    "text-caption text-warning"
                ).props("data-testid=console-link-setup-prompt")
                return
            with ui.row().classes("items-center gap-1"):
                ui.icon("bluetooth").classes("text-blue")
                ui.label(module).classes("text-body2").props(
                    "data-testid=console-link-module"
                )
            ui.label(MODULE_NOTE).classes("text-caption text-grey-5")

    def _render_stages(self, stages: list[ConnectStageResult] | None) -> None:
        if self._stage_list is None:
            return
        self._stage_list.clear()
        with self._stage_list:
            for row in stage_rows(stages):
                icon, color = _STAGE_ICON[row.status]
                with ui.row().classes("items-center gap-1 no-wrap"):
                    ui.icon(icon, color=color)
                    ui.label(row.label).classes("text-caption text-weight-medium")
                    if row.detail:
                        tone = (
                            "text-negative"
                            if row.status is ConnectStageStatus.FAILED
                            else "text-grey-5"
                        )
                        ui.label(row.detail).classes(f"text-caption {tone}")
