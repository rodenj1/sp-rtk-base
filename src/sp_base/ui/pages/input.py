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

from sp_base.models.config_models import InputProfile
from sp_base.services import get_config_service
from sp_base.services.drivers.base import GpsReceiverDriver
from sp_base.ui.layout import page_layout
from sp_base.ui.validators import (
    FieldDef,
    numeric_validation,
    port_validation,
    required,
)

logger = logging.getLogger(__name__)

SOURCE_TYPES = ["tcp", "serial", "bluetooth"]

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
DEFAULT_BAUD = 115200

# TCP-only field definitions (serial and bluetooth have custom UI)
TCP_FIELDS: list[FieldDef] = [
    ("host", "Host", "127.0.0.1", required("Host")),
    ("port", "Port", "5015", port_validation()),
]


def _try_import_bluetooth_manager() -> type | None:
    """Attempt to import BluetoothManager from sp-base-relay.

    Returns the class if available, None otherwise (dbus-fast not installed).
    """
    try:
        from sp_base_relay.core.bluetooth_manager import (  # type: ignore[import-untyped]
            BluetoothManager,
        )
        return BluetoothManager  # type: ignore[no-any-return]
    except (ImportError, Exception):
        return None


@ui.page("/input")
def input_page() -> None:
    """Render the input source configuration page."""
    config_svc = get_config_service()

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

            # For bluetooth: address input + channel input + scan results
            bt_state: dict[str, Any] = {
                "address_input": None,
                "channel_input": None,
                "pin_input": None,
                "bt_manager": None,
                "test_status_label": None,
                "scan_container": None,
                "scan_results": [],
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

                    refresh_btn = ui.button(
                        "", icon="refresh",
                    ).props(
                        "flat round color=white"
                    ).tooltip("Refresh serial port list")

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
                            options[p.port] = (
                                f"{p.port} — {p.description}{star}"
                            )
                        ps.options = options  # type: ignore[assignment]
                        ps.update()

                        # Auto-select first GPS port or first port
                        if ports:
                            ps.value = ports[0].port
                    except Exception as exc:
                        logger.warning("Failed to list serial ports: %s", exc)
                        ui.notify(
                            f"Port scan failed: {exc}", type="warning"
                        )

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
                    with ui.card().classes("w-full q-pa-sm q-mb-sm").style(
                        "background-color: #1a1a2e"
                    ):
                        ui.label("Device Discovery").classes(
                            "text-subtitle2 text-grey-3"
                        )

                        with ui.row().classes("gap-2 items-center q-mt-xs"):
                            scan_btn = ui.button(
                                "Scan for Devices", icon="bluetooth_searching"
                            ).props("color=info outline")
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
                            scan_btn.disable()
                            scan_spinner.set_visibility(True)
                            scan_status.text = "Scanning (10s)..."
                            scan_results_container.clear()
                            bt_state["scan_results"] = []

                            try:
                                mgr = await asyncio.to_thread(
                                    bt_manager_cls  # type: ignore[misc]
                                )
                                bt_state["bt_manager"] = mgr

                                # Get managed objects to list known/discovered devices
                                devices = await asyncio.to_thread(
                                    _discover_bluetooth_devices, mgr
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

                                            with ui.card().classes(
                                                "w-full q-pa-xs cursor-pointer"
                                            ).style(
                                                "background-color: #252540"
                                            ).on("click", _pick_device):
                                                with ui.row().classes(
                                                    "items-center gap-2"
                                                ):
                                                    ui.icon(
                                                        "bluetooth"
                                                    ).classes(
                                                        "text-blue text-body1"
                                                    )
                                                    with ui.column().classes(
                                                        "gap-0"
                                                    ):
                                                        ui.label(
                                                            dev["name"]
                                                            or "Unknown"
                                                        ).classes(
                                                            "text-white "
                                                            "text-caption"
                                                        )
                                                        ui.label(
                                                            dev["mac"]
                                                        ).classes(
                                                            "text-grey-5 "
                                                            "text-caption"
                                                        )
                                                        if dev.get("paired"):
                                                            ui.badge(
                                                                "Paired"
                                                            ).props(
                                                                "color=positive "
                                                                "outline"
                                                            )

                                scan_status.text = (
                                    f"Found {len(devices)} device(s)"
                                )

                            except Exception as exc:
                                logger.warning("Bluetooth scan failed: %s", exc)
                                scan_status.text = f"Scan failed: {exc}"
                                with scan_results_container:
                                    ui.label(
                                        f"Scan error: {exc}"
                                    ).classes("text-negative text-caption")
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

                # ---- Address + Channel + PIN fields ----
                saved_address = ""
                saved_channel = "1"
                saved_pin = "0000"
                if current_input and current_input.source == "bluetooth":
                    saved_address = str(
                        current_input.config.get("mac_address", "")
                    )
                    saved_channel = str(
                        current_input.config.get("channel", "1")
                    )
                    saved_pin = str(
                        current_input.config.get("pin", "0000")
                    )

                addr_input = ui.input(
                    "Device Address (MAC)",
                    value=saved_address,
                    placeholder="e.g. 00:11:22:33:44:55",
                    validation=required("Device address"),
                ).classes("w-full")
                bt_state["address_input"] = addr_input

                with ui.row().classes("w-full gap-4"):
                    chan_input = ui.input(
                        "RFCOMM Channel",
                        value=saved_channel,
                        validation=numeric_validation("Channel"),
                    ).classes("col-grow")
                    bt_state["channel_input"] = chan_input

                    pin_input = ui.input(
                        "PIN Code",
                        value=saved_pin,
                        placeholder="0000",
                    ).classes("col-grow")
                    bt_state["pin_input"] = pin_input

                # ---- Test Connection section ----
                if bt_available:
                    with ui.row().classes("gap-2 items-center q-mt-sm"):
                        test_btn = ui.button(
                            "Test Connection", icon="cable"
                        ).props("color=positive outline")
                        test_spinner = ui.spinner(size="sm")
                        test_spinner.set_visibility(False)

                    test_status_label = ui.label("").classes("q-mt-xs")
                    test_status_label.set_visibility(False)
                    bt_state["test_status_label"] = test_status_label

                    async def _test_bluetooth_connection() -> None:
                        """Test pair + trust + RFCOMM discovery."""
                        mac = str(addr_input.value or "").strip()
                        if not mac:
                            ui.notify(
                                "Enter a device address first",
                                type="warning",
                            )
                            return

                        test_btn.disable()
                        test_spinner.set_visibility(True)
                        test_status_label.set_visibility(True)
                        test_status_label.text = (
                            "Testing connection..."
                        )
                        test_status_label.classes(
                            replace="text-warning q-mt-xs"
                        )

                        try:
                            mgr = bt_state.get("bt_manager")
                            if mgr is None:
                                mgr = await asyncio.to_thread(
                                    bt_manager_cls  # type: ignore[misc]
                                )
                                bt_state["bt_manager"] = mgr

                            # ensure_device_ready: pair + trust + RFCOMM
                            result_mac, channel = await asyncio.to_thread(
                                mgr.ensure_device_ready,  # type: ignore[union-attr]
                                None,  # device_name
                                mac,  # mac_address
                            )

                            # Auto-fill channel
                            chan_input.value = str(channel)

                            test_status_label.text = (
                                f"✓ Connection successful! "
                                f"Device: {result_mac}, "
                                f"RFCOMM Channel: {channel}"
                            )
                            test_status_label.classes(
                                replace="text-positive q-mt-xs"
                            )
                            ui.notify(
                                "Bluetooth test connection successful!",
                                type="positive",
                            )

                        except Exception as exc:
                            logger.warning(
                                "Bluetooth test connection failed: %s", exc
                            )
                            test_status_label.text = (
                                f"✗ Connection failed: {exc}"
                            )
                            test_status_label.classes(
                                replace="text-negative q-mt-xs"
                            )
                            ui.notify(
                                f"Test connection failed: {exc}",
                                type="negative",
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
                            test_btn.enable()
                            test_spinner.set_visibility(False)

                    test_btn.on_click(_test_bluetooth_connection)

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
                bt_state["channel_input"] = None
                bt_state["pin_input"] = None
                bt_state["bt_manager"] = None

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

            def _save_input() -> None:
                """Save input source configuration."""
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
                            return
                    config = {
                        k: v.value
                        for k, v in tcp_inputs.items()
                        if v.value
                    }

                elif src == "serial":
                    port_widget = serial_port_select.get("widget")
                    baud_widget = serial_baud_select.get("widget")
                    port_val = str(
                        port_widget.value if port_widget else ""
                    )
                    baud_val = str(
                        baud_widget.value if baud_widget else DEFAULT_BAUD
                    )
                    if not port_val:
                        ui.notify(
                            "Select a serial port", type="warning"
                        )
                        return
                    config = {"port": port_val, "baud_rate": baud_val}

                elif src == "bluetooth":
                    addr_inp = bt_state.get("address_input")
                    pin_inp = bt_state.get("pin_input")

                    addr_val = str(
                        addr_inp.value if addr_inp else ""
                    ).strip()
                    pin_val = str(
                        pin_inp.value if pin_inp else "0000"
                    ).strip()

                    if not addr_val:
                        ui.notify(
                            "Enter a Bluetooth device address",
                            type="warning",
                        )
                        return
                    config = {
                        "mac_address": addr_val,
                        "pin": pin_val,
                    }

                try:
                    profile = InputProfile(
                        source=src,  # type: ignore[arg-type]
                        config=config,
                    )
                    config_svc.save_input_config(profile)
                    ui.notify("Input source saved ✓", type="positive")
                except Exception as exc:
                    logger.exception("Failed to save input config")
                    ui.notify(
                        f"Error saving input: {exc}", type="negative"
                    )

            ui.button(
                "Save Input Config", icon="save", on_click=_save_input
            ).props("color=primary").classes("q-mt-md")


# ---------------------------------------------------------------------------
# Bluetooth discovery helper (runs in thread via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _discover_bluetooth_devices(
    mgr: Any,
) -> list[dict[str, str]]:
    """Discover Bluetooth devices using BluetoothManager.

    Scans for nearby devices and also includes already-known/paired devices.
    Called via ``asyncio.to_thread()`` since BluetoothManager is sync.

    Args:
        mgr: A BluetoothManager instance.

    Returns:
        List of dicts with 'name', 'mac', 'paired' keys.
    """
    import asyncio as _asyncio

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
                        await _asyncio.sleep(8)  # scan for 8 seconds
                    except Exception:
                        pass  # May fail if already scanning
                    try:
                        await mgr._adapter.call_stop_discovery()  # type: ignore[attr-defined]
                    except Exception:
                        pass

                # Get all managed objects (includes discovered + paired)
                root_intro = await mgr._get_introspection("/")
                manager_proxy = mgr._bus.get_proxy_object(
                    "org.bluez", "/", root_intro
                )
                obj_manager = manager_proxy.get_interface(
                    "org.freedesktop.DBus.ObjectManager"
                )
                raw_objects = await obj_manager.call_get_managed_objects()  # type: ignore[attr-defined]

                for _path, interfaces in raw_objects.items():
                    if "org.bluez.Device1" in interfaces:
                        props = interfaces["org.bluez.Device1"]
                        name = mgr._unwrap_variant(
                            props.get("Name")
                        )
                        address = mgr._unwrap_variant(
                            props.get("Address")
                        )
                        paired = mgr._unwrap_variant(
                            props.get("Paired", False)
                        )
                        if address:
                            result.append({
                                "name": str(name or "Unknown"),
                                "mac": str(address),
                                "paired": "yes" if paired else "",
                            })

            except Exception as exc:
                logger.debug("Error getting BT devices: %s", exc)

            return result

        # Dispatch to the manager's background event loop
        future = _asyncio.run_coroutine_threadsafe(
            _get_devices(), mgr._loop
        )
        devices = future.result(timeout=30)

    except Exception as exc:
        logger.warning("Bluetooth device discovery failed: %s", exc)

    # Sort: named + paired first, named + unpaired second, "Unknown" last
    devices.sort(
        key=lambda d: (
            d["name"] == "Unknown",   # Unknown → bottom
            not d.get("paired"),      # Paired → top
            d["name"].lower(),        # Alphabetical within groups
        )
    )

    return devices
