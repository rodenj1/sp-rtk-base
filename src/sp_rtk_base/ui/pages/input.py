"""Input page — RTCM input source configuration.

Provides forms for configuring the RTCM input source
(serial, TCP, Bluetooth). This is the first thing an operator configures.

Serial mode: dropdown with detected ports (GPS auto-detect, ⭐ markers).
Bluetooth mode: device scan, PIN entry, test-connection workflow.
TCP mode: simple host/port fields.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportOptionalMemberAccess=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nicegui import ui

from sp_rtk_base.models.bluetooth_models import normalize_pin
from sp_rtk_base.models.config_models import InputProfile
from sp_rtk_base.services import (
    get_bluetooth_verification_service,
    get_config_service,
    get_relay_service,
)
from sp_rtk_base.services.bluetooth_service import VerificationRefusedError
from sp_rtk_base.services.drivers.base import GpsReceiverDriver
from sp_rtk_base.ui.bluetooth_status import (
    GreenLostReason,
    HeldGreen,
    StatusLine,
    countdown_label,
    describe_green_lost,
    describe_refusal,
    describe_result,
)
from sp_rtk_base.ui.layout import page_layout
from sp_rtk_base.ui.validators import (
    FieldDef,
    port_validation,
    required,
)

logger = logging.getLogger(__name__)

SOURCE_TYPES = ["tcp", "serial", "bluetooth"]

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
DEFAULT_BAUD = 115200

# Bluetooth discovery scan durations (seconds).
# Some GPS receivers advertise on long intervals (1.2-2.0 s) and can miss
# a short scan window; offer the operator a few presets and default to a
# duration that comfortably covers the slowest realistic advertiser.
BT_SCAN_DURATIONS_SECONDS: list[int] = [20, 30, 45, 60]
DEFAULT_BT_SCAN_DURATION_SECONDS: int = 20


# TCP-only field definitions (serial and bluetooth have custom UI)
TCP_FIELDS: list[FieldDef] = [
    ("host", "Host", "127.0.0.1", required("Host")),
    ("port", "Port", "5015", port_validation()),
]


def _try_import_bluetooth_manager() -> type | None:
    """Attempt to import BluetoothManager from sp-rtk-base-relay.

    Returns the class if available, None otherwise (dbus-fast not installed).
    """
    try:
        from sp_rtk_base_relay.core.bluetooth_manager import (  # type: ignore[import-untyped]
            BluetoothManager,
        )

        return BluetoothManager  # type: ignore[no-any-return]
    except (ImportError, Exception):
        return None


@ui.page("/input")
def input_page() -> None:
    """Render the input source configuration page."""
    config_svc = get_config_service()
    verification_svc = get_bluetooth_verification_service()
    relay_svc = get_relay_service()

    with page_layout("Input"):
        ui.label("Input Source").classes("text-h4 text-white q-mb-md")

        with ui.card().classes("w-full q-pa-md"):
            ui.label("RTCM Input Source").classes("text-h6 text-white")
            ui.separator()
            ui.label(
                "Configure where RTCM correction data is read from. "
                "This is the first step before starting the relay."
            ).classes("text-grey-4 q-mt-xs text-caption")

            current_input = config_svc.get_input_config()
            current_source = current_input.source if current_input else "tcp"

            source_select = ui.select(
                SOURCE_TYPES,
                label="Source Type",
                value=current_source,
            ).classes("w-full q-mt-sm")

            # Container for source-specific fields
            fields_container = ui.column().classes("w-full gap-1 q-mt-sm")

            # ============================================================
            # Shared state — populated by the active source section
            # ============================================================
            # For TCP: standard text inputs keyed by field name
            tcp_inputs: dict[str, ui.input] = {}

            # For serial: select + baud select
            serial_port_select: dict[str, Any] = {}  # "widget" key
            serial_baud_select: dict[str, Any] = {}

            # For bluetooth: address + PIN inputs, scan results, and the
            # Green currently in hand (a :class:`HeldGreen`, or None).
            bt_state: dict[str, Any] = {
                "address_input": None,
                "pin_input": None,
                "bt_manager": None,
                "test_status_label": None,
                "scan_container": None,
                "scan_results": [],
                "held_green": None,
                "green_timer": None,
            }

            # ============================================================
            # Source field builders
            # ============================================================

            def _build_tcp_fields() -> None:
                """Build TCP source fields (host + port)."""
                tcp_inputs.clear()
                for fname, flabel, fdefault, fvalidation in TCP_FIELDS:
                    current_val = ""
                    if current_input and current_input.source == "tcp":
                        current_val = str(current_input.config.get(fname, ""))
                    inp = ui.input(
                        flabel,
                        value=current_val or fdefault,
                        validation=fvalidation,
                    ).classes("w-full")
                    tcp_inputs[fname] = inp

            def _build_serial_fields() -> None:
                """Build serial source fields with port dropdown + GPS detection."""
                serial_port_select.clear()
                serial_baud_select.clear()

                ui.label(
                    "Select the serial port for RTCM data. "
                    "Ports marked with ⭐ are likely GPS receivers."
                ).classes("text-grey-4 text-caption")

                with ui.row().classes("w-full gap-2 items-end"):
                    ps = ui.select(
                        options=[],
                        label="Serial Port",
                        with_input=True,
                    ).classes("col-grow")
                    serial_port_select["widget"] = ps

                    refresh_btn = (
                        ui.button(
                            "",
                            icon="refresh",
                        )
                        .props("flat round color=white")
                        .tooltip("Refresh serial port list")
                    )

                bs = ui.select(
                    options={r: str(r) for r in BAUD_RATES},
                    label="Baud Rate",
                    value=DEFAULT_BAUD,
                ).classes("w-full")
                serial_baud_select["widget"] = bs

                def _refresh_serial_ports() -> None:
                    """Reload serial ports from the system."""
                    try:
                        ports = GpsReceiverDriver.list_serial_ports()
                        options: dict[str, str] = {}
                        for p in ports:
                            star = " ⭐" if p.is_gps else ""
                            options[p.port] = f"{p.port} — {p.description}{star}"
                        ps.options = options  # type: ignore[assignment]
                        ps.update()

                        # Auto-select first GPS port or first port
                        if ports:
                            ps.value = ports[0].port
                    except Exception as exc:
                        logger.warning("Failed to list serial ports: %s", exc)
                        ui.notify(f"Port scan failed: {exc}", type="warning")

                refresh_btn.on_click(lambda: _refresh_serial_ports())

                # Initial load
                _refresh_serial_ports()

                # Restore saved values
                if current_input and current_input.source == "serial":
                    saved_port = current_input.config.get("port", "")
                    saved_baud = current_input.config.get("baud_rate", "")
                    if saved_port:
                        ps.value = saved_port
                    if saved_baud:
                        bs.value = int(saved_baud)

            def _build_bluetooth_fields() -> None:
                """Build Bluetooth source fields with scan + test connection."""
                bt_manager_cls = _try_import_bluetooth_manager()
                bt_available = bt_manager_cls is not None

                if not bt_available:
                    ui.label(
                        "⚠ Bluetooth support requires dbus-fast. "
                        "Install with: pip install dbus-fast"
                    ).classes("text-warning q-mb-sm")
                    ui.label(
                        "You can still enter the device address manually below."
                    ).classes("text-grey-4 text-caption q-mb-sm")
                else:
                    ui.label(
                        "Scan for nearby Bluetooth devices or enter the "
                        "address manually. Use Test Connection to verify "
                        "pairing before saving."
                    ).classes("text-grey-4 text-caption q-mb-sm")

                # ---- Scan section (only if dbus-fast available) ----
                if bt_available:
                    with (
                        ui.card()
                        .classes("w-full q-pa-sm q-mb-sm")
                        .style("background-color: #1a1a2e")
                    ):
                        ui.label("Device Discovery").classes(
                            "text-subtitle2 text-grey-3"
                        )

                        with ui.row().classes("gap-2 items-center q-mt-xs"):
                            scan_btn = ui.button(
                                "Scan for Devices", icon="bluetooth_searching"
                            ).props("color=info outline")
                            scan_duration_select = (
                                ui.select(
                                    options={
                                        d: f"{d} s" for d in BT_SCAN_DURATIONS_SECONDS
                                    },
                                    value=DEFAULT_BT_SCAN_DURATION_SECONDS,
                                    label="Scan duration",
                                )
                                .props("dense outlined")
                                .style("min-width: 110px")
                                .tooltip(
                                    "How long to scan. Increase for slow-"
                                    "advertising devices (default 20 s)."
                                )
                            )
                            scan_spinner = ui.spinner(size="sm")
                            scan_spinner.set_visibility(False)
                            scan_status = ui.label("").classes(
                                "text-caption text-grey-5"
                            )

                        scan_results_container = ui.column().classes(
                            "w-full gap-1 q-mt-xs"
                        )
                        bt_state["scan_container"] = scan_results_container

                        async def _scan_bluetooth() -> None:
                            """Scan for Bluetooth devices using BluetoothManager."""
                            # Resolve scan duration from the dropdown (fall back
                            # to the default if the widget is somehow unset).
                            try:
                                scan_seconds = int(
                                    scan_duration_select.value
                                    or DEFAULT_BT_SCAN_DURATION_SECONDS
                                )
                            except (TypeError, ValueError):
                                scan_seconds = DEFAULT_BT_SCAN_DURATION_SECONDS
                            if scan_seconds <= 0:
                                scan_seconds = DEFAULT_BT_SCAN_DURATION_SECONDS

                            scan_btn.disable()
                            scan_duration_select.disable()
                            scan_spinner.set_visibility(True)
                            scan_status.text = f"Scanning ({scan_seconds}s)..."
                            scan_results_container.clear()
                            bt_state["scan_results"] = []

                            try:
                                mgr = await asyncio.to_thread(
                                    bt_manager_cls  # type: ignore[misc]
                                )
                                bt_state["bt_manager"] = mgr

                                # Get managed objects to list known/discovered devices
                                devices = await asyncio.to_thread(
                                    _discover_bluetooth_devices,
                                    mgr,
                                    scan_seconds,
                                )

                                bt_state["scan_results"] = devices

                                with scan_results_container:
                                    if not devices:
                                        ui.label(
                                            "No devices found. Ensure "
                                            "your device is in pairing mode."
                                        ).classes(
                                            "text-grey-5 text-italic text-caption"
                                        )
                                    else:
                                        for dev in devices:
                                            _dev = dev

                                            def _pick_device(
                                                d: dict[str, str] = _dev,
                                            ) -> None:
                                                addr_input = bt_state.get(
                                                    "address_input"
                                                )
                                                if addr_input:
                                                    addr_input.value = d["mac"]
                                                ui.notify(
                                                    f"Selected: {d['name']} "
                                                    f"({d['mac']})",
                                                    type="info",
                                                )

                                            with (
                                                ui.card()
                                                .classes(
                                                    "w-full q-pa-xs cursor-pointer"
                                                )
                                                .style("background-color: #252540")
                                                .on("click", _pick_device)
                                            ):
                                                with ui.row().classes(
                                                    "items-center gap-2"
                                                ):
                                                    ui.icon("bluetooth").classes(
                                                        "text-blue text-body1"
                                                    )
                                                    with ui.column().classes("gap-0"):
                                                        ui.label(
                                                            dev["name"] or "Unknown"
                                                        ).classes(
                                                            "text-white text-caption"
                                                        )
                                                        ui.label(dev["mac"]).classes(
                                                            "text-grey-5 text-caption"
                                                        )
                                                        if dev.get("paired"):
                                                            ui.badge("Paired").props(
                                                                "color=positive outline"
                                                            )

                                scan_status.text = f"Found {len(devices)} device(s)"

                            except Exception as exc:
                                logger.warning("Bluetooth scan failed: %s", exc)
                                scan_status.text = f"Scan failed: {exc}"
                                with scan_results_container:
                                    ui.label(f"Scan error: {exc}").classes(
                                        "text-negative text-caption"
                                    )
                            finally:
                                # Release Bluetooth resources so relay can use the device
                                _mgr = bt_state.get("bt_manager")
                                if _mgr is not None:
                                    try:
                                        await asyncio.to_thread(_mgr.close)
                                    except Exception:
                                        pass
                                    bt_state["bt_manager"] = None
                                scan_btn.enable()
                                scan_spinner.set_visibility(False)

                        scan_btn.on_click(_scan_bluetooth)

                # ---- Address + PIN fields ----
                #
                # There is no "RFCOMM Channel" field.  It was removed in
                # #131: it was never persisted, it read a config key
                # nothing ever wrote (so it always showed 1), and the
                # relay's ``BluetoothConfig`` has no channel parameter,
                # so the value could not have been honoured even if it
                # had been saved.  ``discover_rfcomm_channel`` is a stub
                # ``return 1``, so there is nothing for an operator to
                # choose.  The channel a Verification actually used is
                # reported as a detail on its ``connect`` Stage — and on
                # ``rfcomm_channel`` in the API response — rather than
                # as a field that pretends to be a control.
                saved_address = ""
                saved_pin = "0000"
                if current_input and current_input.source == "bluetooth":
                    saved_address = str(current_input.config.get("mac_address", ""))
                    saved_pin = str(current_input.config.get("pin", "0000"))

                addr_input = ui.input(
                    "Device Address (MAC)",
                    value=saved_address,
                    placeholder="e.g. 00:11:22:33:44:55",
                    validation=required("Device address"),
                ).classes("w-full")
                bt_state["address_input"] = addr_input

                pin_input = ui.input(
                    "PIN Code",
                    value=saved_pin,
                    placeholder="0000",
                ).classes("w-full")
                bt_state["pin_input"] = pin_input

                # ---- Test Connection section ----
                if bt_available:
                    with ui.row().classes("gap-2 items-center q-mt-sm"):
                        test_btn = ui.button("Test Connection", icon="cable").props(
                            "color=positive outline"
                        )
                        test_spinner = ui.spinner(size="sm")
                        test_spinner.set_visibility(False)

                    test_status_label = ui.label("").classes("q-mt-xs")
                    test_status_label.set_visibility(False)
                    bt_state["test_status_label"] = test_status_label

                    # The Green's remaining life and the action it
                    # entitles the operator to, shown together: the
                    # countdown exists to tell them how long the offer
                    # stands, so it belongs beside the offer.
                    with ui.row().classes(
                        "gap-2 items-center q-mt-xs"
                    ) as green_actions_row:
                        save_start_btn = ui.button(
                            "Save & Start now →", icon="play_arrow"
                        ).props("color=positive flat dense")
                        countdown_lbl = ui.label("").classes("text-caption text-grey-7")
                    green_actions_row.set_visibility(False)

                    def _current_values() -> tuple[str, str]:
                        """The MAC and PIN as they stand in the form now."""
                        return (
                            str(addr_input.value or "").strip(),
                            str(pin_input.value or ""),
                        )

                    def _clear_green() -> None:
                        """Drop the held Green and hide what it entitled."""
                        bt_state["held_green"] = None
                        green_actions_row.set_visibility(False)

                    def _green_loss_reason() -> GreenLostReason | None:
                        """Why the held Green no longer stands, if it doesn't."""
                        held = bt_state.get("held_green")
                        if held is None:
                            return None
                        mac, pin = _current_values()
                        return held.loss_reason(mac, pin)

                    def _tick_green() -> None:
                        """Refresh the countdown; retire the Green when void.

                        Runs once a second so expiry is visible.  Edits
                        are handled by the field handlers below too, so
                        an edited Green dies at once rather than up to a
                        second later.
                        """
                        held = bt_state.get("held_green")
                        if held is None:
                            return
                        reason = _green_loss_reason()
                        if reason is not None:
                            _lose_green(reason)
                            return
                        label = countdown_label(held.result.expires_at)
                        if label is not None:
                            countdown_lbl.text = f"expires in {label}"

                    def _lose_green(reason: GreenLostReason) -> None:
                        """Retire the Green and say which way it died.

                        The two causes get different sentences for the
                        same reason the two Warnings do: "the clock ran
                        out" and "you changed the PIN" are different
                        facts, and an operator who reached for "Save &
                        Start now →" and found it gone needs to know
                        which one happened.
                        """
                        _clear_green()
                        _set_status(describe_green_lost(reason))

                    def _on_field_edited() -> None:
                        """Any edit to the MAC or PIN voids the Green.

                        The Green promises that Save and Start will
                        connect *with these values*; once they change it
                        is a promise about something the operator is no
                        longer looking at.
                        """
                        reason = _green_loss_reason()
                        if reason is not None:
                            _lose_green(reason)

                    addr_input.on("update:model-value", lambda _: _on_field_edited())
                    pin_input.on("update:model-value", lambda _: _on_field_edited())

                    # Switching source away and back rebuilds this whole
                    # section, so cancel the previous tick explicitly
                    # rather than trusting the container teardown to
                    # collect it.  A leaked timer would keep writing to
                    # the labels of a section that no longer exists,
                    # once a second, for the life of the page.
                    prior_timer = bt_state.get("green_timer")
                    if prior_timer is not None:
                        try:
                            prior_timer.cancel()
                        except Exception:
                            logger.debug("Prior Green timer was already cancelled")
                    bt_state["green_timer"] = ui.timer(1.0, _tick_green)

                    def _set_status(line: StatusLine) -> None:
                        test_status_label.set_visibility(True)
                        test_status_label.text = line.text
                        test_status_label.classes(replace=f"text-{line.tone} q-mt-xs")

                    async def _run_verification(confirm_repair: bool = False) -> None:
                        """Run a Verification against the form's values.

                        Args:
                            confirm_repair: The operator has seen the
                                force-repair dialog and agreed.
                        """
                        mac, pin = _current_values()
                        if not mac:
                            ui.notify("Enter a device address first", type="warning")
                            return

                        _clear_green()
                        test_btn.disable()
                        test_spinner.set_visibility(True)
                        test_status_label.set_visibility(True)
                        test_status_label.text = "Testing connection…"
                        test_status_label.classes(replace="text-warning q-mt-xs")

                        try:
                            result = await verification_svc.verify(
                                mac_address=mac,
                                pin=pin,
                                confirm_repair=confirm_repair,
                            )
                        except VerificationRefusedError as exc:
                            # Three refusals share HTTP 409 with
                            # unrelated remedies, so branch on the code.
                            # This one is answered by a dialog rather
                            # than by the status line, because it is a
                            # question, not a verdict.
                            if exc.code == "repair_confirmation_required":
                                test_status_label.set_visibility(False)
                                _confirm_repair_dialog.open()
                            else:
                                _set_status(describe_refusal(exc))
                            return
                        except Exception as exc:
                            logger.exception("Verification failed unexpectedly")
                            _set_status(
                                StatusLine(
                                    text=f"The test could not run: {exc}",
                                    tone="negative",
                                )
                            )
                            return
                        finally:
                            test_btn.enable()
                            test_spinner.set_visibility(False)

                        _set_status(describe_result(result))

                        if result.verdict == "green":
                            bt_state["held_green"] = HeldGreen(
                                result=result,
                                mac_address=mac,
                                pin=normalize_pin(pin),
                            )
                            countdown_lbl.text = ""
                            green_actions_row.set_visibility(True)
                            _tick_green()

                    with ui.dialog() as _confirm_repair_dialog, ui.card():
                        ui.label("Re-pair with this PIN?").classes("text-subtitle1")
                        ui.label(
                            "This will remove the existing pairing and re-pair "
                            "with the PIN you entered. If the PIN is wrong, the "
                            "device will be left unpaired."
                        ).classes("text-body2")
                        # Stated here rather than as a standing UI wart:
                        # this is the only moment the operator can weigh
                        # the risk, with the device in front of them.
                        with ui.row().classes("justify-end gap-2 w-full"):
                            ui.button(
                                "Cancel",
                                on_click=_confirm_repair_dialog.close,
                            ).props("flat")

                            async def _confirm_and_retest() -> None:
                                _confirm_repair_dialog.close()
                                await _run_verification(confirm_repair=True)

                            ui.button(
                                "Remove pairing and test",
                                on_click=_confirm_and_retest,
                            ).props("color=negative")

                    async def _save_and_start() -> None:
                        """Save the Verified config, then start the relay."""
                        reason = _green_loss_reason()
                        if reason is not None or bt_state.get("held_green") is None:
                            _lose_green(reason or "expired")
                            return
                        if not _save_input():
                            return
                        try:
                            config = config_svc.get_config()
                            enabled = [d for d in config.destinations if d.enabled]
                            if not enabled:
                                ui.notify(
                                    "No enabled destinations — add one in "
                                    "Outputs first",
                                    type="warning",
                                )
                                return
                            if config.input is None:
                                ui.notify("No input configured", type="warning")
                                return
                            await relay_svc.start_relay(
                                config.input.to_relay_config(),
                                [d.to_relay_config() for d in enabled],
                                trigger="verification-handoff",
                            )
                            ui.notify("Relay started ✓", type="positive")
                            _clear_green()
                        except Exception as exc:
                            logger.exception("Start after Verification failed")
                            ui.notify(
                                f"Could not start the relay: {exc}", type="negative"
                            )

                    save_start_btn.on_click(_save_and_start)
                    test_btn.on_click(lambda: _run_verification())

            # ============================================================
            # Source type change handler
            # ============================================================

            def _update_source_fields() -> None:
                """Update config fields when source type changes."""
                fields_container.clear()
                tcp_inputs.clear()
                serial_port_select.clear()
                serial_baud_select.clear()
                bt_state["address_input"] = None
                bt_state["pin_input"] = None
                bt_state["bt_manager"] = None
                # A Green describes a specific device; switching source
                # away from Bluetooth leaves nothing for it to describe.
                bt_state["held_green"] = None

                src = source_select.value or "tcp"
                with fields_container:
                    if src == "tcp":
                        _build_tcp_fields()
                    elif src == "serial":
                        _build_serial_fields()
                    elif src == "bluetooth":
                        _build_bluetooth_fields()

            source_select.on_value_change(lambda _: _update_source_fields())
            _update_source_fields()

            # ============================================================
            # Save handler
            # ============================================================

            def _save_input() -> bool:
                """Save input source configuration.

                Returns:
                    ``True`` when the configuration was persisted.  The
                    "Save & Start now →" action needs this: starting the
                    relay after a save that bailed on a validation error
                    would start it on the *previous* configuration, which
                    is not the one the operator just had verified.
                """
                src = source_select.value or "tcp"

                # Gather config values based on source type
                config: dict[str, Any] = {}

                if src == "tcp":
                    # Validate TCP inputs
                    for inp in tcp_inputs.values():
                        if inp.error:
                            ui.notify(
                                "Fix validation errors before saving",
                                type="warning",
                            )
                            return False
                    # NiceGUI ui.input() returns strings.  The TCP port
                    # must be an integer for the relay's InputConfig
                    # validation to accept it — coerce here.  Without
                    # this, the saved config has port="5015" and
                    # ``relay.start()`` fails with "port must be an
                    # integer between 1 and 65535" forever after.
                    config = {}
                    for k, v in tcp_inputs.items():
                        if not v.value:
                            continue
                        if k == "port":
                            try:
                                config[k] = int(v.value)
                            except (TypeError, ValueError):
                                ui.notify(
                                    "Port must be a whole number (1-65535)",
                                    type="warning",
                                )
                                return False
                        else:
                            config[k] = v.value

                elif src == "serial":
                    port_widget = serial_port_select.get("widget")
                    baud_widget = serial_baud_select.get("widget")
                    port_val = str(port_widget.value if port_widget else "")
                    baud_val = str(baud_widget.value if baud_widget else DEFAULT_BAUD)
                    if not port_val:
                        ui.notify("Select a serial port", type="warning")
                        return False
                    config = {"port": port_val, "baud_rate": baud_val}

                elif src == "bluetooth":
                    addr_inp = bt_state.get("address_input")
                    pin_inp = bt_state.get("pin_input")

                    addr_val = str(addr_inp.value if addr_inp else "").strip()
                    pin_val = str(pin_inp.value if pin_inp else "0000").strip()

                    if not addr_val:
                        ui.notify(
                            "Enter a Bluetooth device address",
                            type="warning",
                        )
                        return False
                    config = {
                        "mac_address": addr_val,
                        "pin": pin_val,
                    }

                # A Green in hand is what entitles this save to record
                # a Proven PIN.  The record is still corroborated
                # server-side against the memo of pairings the server
                # itself performed — passing it here is an offer, not an
                # assertion, and a save that cannot be corroborated
                # simply persists without one.
                proven: dict[str, Any] = {}
                if src == "bluetooth":
                    held = bt_state.get("held_green")
                    mac_now = str(config.get("mac_address", ""))
                    pin_now = str(config.get("pin", ""))
                    if held is not None and held.is_valid_for(mac_now, pin_now):
                        proven = {
                            "proven_pin": held.pin,
                            "pin_proven_at": held.result.verified_at,
                        }

                try:
                    profile = InputProfile(
                        source=src,  # type: ignore[arg-type]
                        config=config,
                        **proven,
                    )
                    # Without the corroborator the service drops every
                    # Proven PIN it is handed — which is correct for
                    # callers with no memo to consult, and wrong here:
                    # the page would silently discard the proof the
                    # Verification just earned, and the next test would
                    # force-repair a Bond that was only just built.
                    config_svc.save_input_config(
                        profile,
                        corroborate=verification_svc.corroborates,
                    )
                    ui.notify("Input source saved ✓", type="positive")
                    return True
                except Exception as exc:
                    logger.exception("Failed to save input config")
                    ui.notify(f"Error saving input: {exc}", type="negative")
                    return False

            ui.button(
                "Save Input Config", icon="save", on_click=lambda: _save_input()
            ).props("color=primary").classes("q-mt-md")


# ---------------------------------------------------------------------------
# Bluetooth discovery helper (runs in thread via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _discover_bluetooth_devices(
    mgr: Any,
    scan_seconds: int = DEFAULT_BT_SCAN_DURATION_SECONDS,
) -> list[dict[str, str]]:
    """Discover Bluetooth devices using BluetoothManager.

    Scans for nearby devices and also includes already-known/paired devices.
    Called via ``asyncio.to_thread()`` since BluetoothManager is sync.

    Args:
        mgr: A BluetoothManager instance.
        scan_seconds: How long to actively scan for advertisements before
            collecting results. Slow-advertising devices may need 30-60 s;
            defaults to :data:`DEFAULT_BT_SCAN_DURATION_SECONDS` (20).

    Returns:
        List of dicts with 'name', 'mac', 'paired' keys.
    """
    import asyncio as _asyncio

    # Clamp to a sane positive duration so a misconfigured caller can't
    # turn the scan into a no-op or hang the page forever.
    if scan_seconds <= 0:
        scan_seconds = DEFAULT_BT_SCAN_DURATION_SECONDS

    devices: list[dict[str, str]] = []

    try:
        # Use the manager's internal async method to get managed objects
        # which includes both known and recently-discovered devices.
        # We need to run the async discovery on the manager's own loop.

        async def _get_devices() -> list[dict[str, str]]:
            result: list[dict[str, str]] = []

            if mgr._bus is None:
                return result

            try:
                # Start a scan to find new devices
                if mgr._adapter is not None:
                    try:
                        await mgr._adapter.call_start_discovery()  # type: ignore[attr-defined]
                        await _asyncio.sleep(scan_seconds)
                    except Exception:
                        pass  # May fail if already scanning
                    try:
                        await mgr._adapter.call_stop_discovery()  # type: ignore[attr-defined]
                    except Exception:
                        pass

                # Get all managed objects (includes discovered + paired)
                root_intro = await mgr._get_introspection("/")
                manager_proxy = mgr._bus.get_proxy_object("org.bluez", "/", root_intro)
                obj_manager = manager_proxy.get_interface(
                    "org.freedesktop.DBus.ObjectManager"
                )
                raw_objects = await obj_manager.call_get_managed_objects()  # type: ignore[attr-defined]

                for _path, interfaces in raw_objects.items():
                    if "org.bluez.Device1" in interfaces:
                        props = interfaces["org.bluez.Device1"]
                        name = mgr._unwrap_variant(props.get("Name"))
                        address = mgr._unwrap_variant(props.get("Address"))
                        paired = mgr._unwrap_variant(props.get("Paired", False))
                        if address:
                            result.append(
                                {
                                    "name": str(name or "Unknown"),
                                    "mac": str(address),
                                    "paired": "yes" if paired else "",
                                }
                            )

            except Exception as exc:
                logger.debug("Error getting BT devices: %s", exc)

            return result

        # Dispatch to the manager's background event loop
        future = _asyncio.run_coroutine_threadsafe(_get_devices(), mgr._loop)
        devices = future.result(timeout=30)

    except Exception as exc:
        logger.warning("Bluetooth device discovery failed: %s", exc)

    # Sort: named + paired first, named + unpaired second, "Unknown" last
    devices.sort(
        key=lambda d: (
            d["name"] == "Unknown",  # Unknown → bottom
            not d.get("paired"),  # Paired → top
            d["name"].lower(),  # Alphabetical within groups
        )
    )

    return devices
