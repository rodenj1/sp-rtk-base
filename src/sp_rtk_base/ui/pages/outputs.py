"""Outputs page — destination management.

Provides a list of configured destinations with add, edit, delete,
and enable/disable controls.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import logging

from nicegui import ui

from sp_rtk_base.models.config_models import DestinationProfile, FilterProfile
from sp_rtk_base.services import get_config_service, get_relay_service
from sp_rtk_base.ui.layout import page_layout
from sp_rtk_base.ui.validators import (
    FieldDef,
    is_non_empty,
    numeric_validation,
    port_validation,
    required,
)

logger = logging.getLogger(__name__)

DEST_TYPES = ["surepath", "ntrip", "tcp_server"]
NTRIP_VERSIONS = ["1.0", "2.0"]


# ---------------------------------------------------------------------------
# Type-specific config field definitions
# ---------------------------------------------------------------------------

TYPE_FIELDS: dict[str, list[FieldDef]] = {
    "surepath": [
        ("host", "Host", "", required("Host")),
        ("port", "Port", "28001", port_validation()),
        ("project_id", "Project ID", "", required("Project ID")),
        ("token", "Auth Token", "", required("Auth Token")),
    ],
    "ntrip": [
        ("caster", "Caster Host", "rtk2go.com", required("Caster Host")),
        ("port", "Port", "2101", port_validation()),
        ("mountpoint", "Mountpoint", "", required("Mountpoint")),
        ("password", "Password", "", required("Password")),
        # version + username are rendered dynamically — see _update_fields / _show_edit_dialog
    ],
    "tcp_server": [
        ("host", "Bind Address", "0.0.0.0", required("Bind Address")),
        ("port", "Port", "5016", port_validation()),
        ("max_clients", "Max Clients", "5", numeric_validation("Max Clients")),
    ],
}

# Legacy field list for display (no validation needed)
TYPE_DISPLAY_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "surepath": [
        ("host", "Host", ""),
        ("port", "Port", "28001"),
        ("project_id", "Project ID", ""),
        ("token", "Auth Token", ""),
    ],
    "ntrip": [
        ("caster", "Caster Host", "rtk2go.com"),
        ("port", "Port", "2101"),
        ("mountpoint", "Mountpoint", ""),
        ("password", "Password", ""),
        ("username", "Username (v2.0)", ""),
        ("version", "NTRIP Version", "1.0"),
    ],
    "tcp_server": [
        ("host", "Bind Address", "0.0.0.0"),
        ("port", "Port", "5016"),
        ("max_clients", "Max Clients", "5"),
    ],
}


@ui.page("/outputs", dark=True)
def outputs_page() -> None:
    """Render the outputs (destinations) management page."""
    config_svc = get_config_service()

    with page_layout("Outputs"):
        ui.label("Destinations").classes("text-h4 text-white q-mb-md")

        # Destination list container
        dest_list = ui.column().classes("w-full gap-3")

        # Add button
        ui.button(
            "Add Destination", icon="add", on_click=lambda: _show_add_dialog()
        ).props("color=primary").classes("q-mt-md")

        def _refresh_list() -> None:
            """Refresh the destination list display."""
            destinations = config_svc.get_destinations()
            dest_list.clear()
            with dest_list:
                if not destinations:
                    ui.label("No destinations configured yet.").classes(
                        "text-grey-6 q-pa-md"
                    )
                    return
                for dest in destinations:
                    _render_dest_card(dest)

        def _render_dest_card(dest: DestinationProfile) -> None:
            """Render a single destination card."""
            with ui.card().classes("w-full q-pa-md"):
                with ui.row().classes("items-center justify-between w-full"):
                    with ui.row().classes("items-center gap-3"):
                        # Type icon
                        icon_map = {
                            "surepath": "cloud_upload",
                            "ntrip": "cell_tower",
                            "tcp_server": "dns",
                        }
                        ui.icon(icon_map.get(dest.type, "output")).classes(
                            "text-blue-4 text-2xl"
                        )
                        with ui.column().classes("gap-0"):
                            ui.label(dest.name).classes(
                                "text-body1 text-white font-bold"
                            )
                            ui.label(dest.type.replace("_", " ").title()).classes(
                                "text-caption text-grey-5"
                            )

                    with ui.row().classes("items-center gap-2"):
                        # Enable/disable toggle
                        ui.switch(
                            value=dest.enabled,
                            on_change=lambda e, n=dest.name: _toggle_enabled(
                                n, e.value
                            ),
                        ).props("color=green")

                        # Edit button
                        ui.button(
                            icon="edit",
                            on_click=lambda _e, d=dest: _show_edit_dialog(d),
                        ).props("flat round color=blue-4 size=sm")

                        # Delete button
                        ui.button(
                            icon="delete",
                            on_click=lambda _e, n=dest.name: _confirm_delete(n),
                        ).props("flat round color=red-4 size=sm")

                # Show key config values
                with ui.row().classes("q-mt-sm gap-4"):
                    for key, val in list(dest.config.items())[:3]:
                        ui.label(f"{key}: {val}").classes("text-caption text-grey-5")

        async def _toggle_enabled(name: str, enabled: object) -> None:
            """Toggle a destination's enabled state.

            Updates the config AND tells the running relay engine
            to start/stop the destination if the relay is active.
            """
            dest = config_svc.get_destination(name)
            if dest is None:
                return
            is_enabled = bool(enabled)
            updated = dest.model_copy(update={"enabled": is_enabled})
            config_svc.save_destination(updated)

            # Tell the running engine to start/stop this destination
            relay_svc = get_relay_service()
            if relay_svc.is_running:
                try:
                    if is_enabled:
                        await relay_svc.start_destination(name)
                        ui.notify(f"Resumed '{name}' in relay engine", type="positive")
                    else:
                        await relay_svc.stop_destination(name)
                        ui.notify(f"Paused '{name}' in relay engine", type="info")
                except Exception as exc:
                    logger.exception("Could not toggle destination in engine")
                    ui.notify(
                        f"Config saved but engine error: {exc}",
                        type="warning",
                    )

            # Defer refresh so the switch's on_change callback finishes
            # before we destroy the container that holds it
            ui.timer(0, _refresh_list, once=True)

        def _confirm_delete(name: str) -> None:
            """Show delete confirmation dialog."""
            with ui.dialog() as dialog, ui.card():
                ui.label(f"Delete destination '{name}'?").classes("text-h6")
                ui.label("This cannot be undone.").classes("text-grey-6")
                with ui.row().classes("justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button(
                        "Delete",
                        on_click=lambda: (_delete_dest(name), dialog.close()),
                    ).props("color=red")
            dialog.open()

        def _delete_dest(name: str) -> None:
            """Delete a destination."""
            config_svc.remove_destination(name)
            ui.notify(f"Deleted '{name}'", type="info")
            # Defer refresh so dialog close completes first
            ui.timer(0, _refresh_list, once=True)

        def _show_add_dialog() -> None:
            """Show the add destination dialog."""
            with (
                ui.dialog() as dialog,
                ui.card().classes("q-pa-md").style("min-width: 400px"),
            ):
                ui.label("Add Destination").classes("text-h6 text-white")
                name_input = ui.input(
                    "Name",
                    validation={"Name is required": is_non_empty},
                ).classes("w-full")
                type_select = ui.select(
                    DEST_TYPES, label="Type", value="tcp_server"
                ).classes("w-full")

                # Dynamic config fields container
                fields_container = ui.column().classes("w-full gap-1")
                config_inputs: dict[str, ui.input | ui.select] = {}

                def _update_fields() -> None:
                    fields_container.clear()
                    config_inputs.clear()
                    dest_type = type_select.value or "tcp_server"
                    with fields_container:
                        for fname, flabel, fdefault, fvalidation in TYPE_FIELDS.get(
                            dest_type, []
                        ):
                            inp = ui.input(
                                flabel,
                                value=fdefault,
                                validation=fvalidation,
                            ).classes("w-full")
                            config_inputs[fname] = inp
                        # NTRIP: version selector + conditional username
                        if dest_type == "ntrip":
                            ver = ui.select(
                                NTRIP_VERSIONS,
                                label="NTRIP Version",
                                value="1.0",
                            ).classes("w-full")
                            config_inputs["version"] = ver
                            # Username container — only visible for v2.0
                            username_container = ui.column().classes("w-full")
                            username_container.set_visibility(False)

                            with username_container:
                                uname = ui.input("Username", value="").classes("w-full")
                                config_inputs["username"] = uname

                            def _on_version_change(e: object) -> None:
                                is_v2 = ver.value == "2.0"
                                username_container.set_visibility(is_v2)

                            ver.on_value_change(_on_version_change)

                type_select.on_value_change(lambda _: _update_fields())
                _update_fields()

                with ui.row().classes("justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button(
                        "Add",
                        on_click=lambda: _save_new(
                            name_input,
                            type_select.value or "tcp_server",
                            config_inputs,
                            dialog,
                        ),
                    ).props("color=primary")
            dialog.open()

        def _save_new(
            name_input: ui.input,
            dest_type: str,
            config_inputs: dict[str, ui.input | ui.select],
            dialog: ui.dialog,
        ) -> None:
            """Save a new destination from dialog inputs."""
            name = name_input.value or ""

            # Validate name
            if not name.strip():
                ui.notify("Name is required", type="warning")
                return
            if name_input.error:
                ui.notify("Fix name validation errors", type="warning")
                return
            if config_svc.get_destination(name) is not None:
                ui.notify(f"'{name}' already exists", type="warning")
                return

            # Validate config fields
            for inp in config_inputs.values():
                if inp.error:
                    ui.notify("Fix validation errors before saving", type="warning")
                    return

            config = {k: v.value for k, v in config_inputs.items() if v.value}
            try:
                profile = DestinationProfile(
                    name=name,
                    type=dest_type,  # type: ignore[arg-type]
                    config=config,
                    filter=FilterProfile(),
                )
                config_svc.save_destination(profile)
                ui.notify(f"Added '{name}'", type="positive")
                dialog.close()
                _refresh_list()
            except Exception as exc:
                logger.exception("Failed to add destination")
                ui.notify(f"Error: {exc}", type="negative")

        def _show_edit_dialog(dest: DestinationProfile) -> None:
            """Show the edit destination dialog."""
            with (
                ui.dialog() as dialog,
                ui.card().classes("q-pa-md").style("min-width: 400px"),
            ):
                ui.label(f"Edit: {dest.name}").classes("text-h6 text-white")

                config_inputs: dict[str, ui.input | ui.select] = {}
                for fname, flabel, _fdefault, fvalidation in TYPE_FIELDS.get(
                    dest.type, []
                ):
                    current = str(dest.config.get(fname, ""))
                    inp = ui.input(
                        flabel,
                        value=current,
                        validation=fvalidation,
                    ).classes("w-full")
                    config_inputs[fname] = inp
                # NTRIP: version selector + conditional username (edit mode)
                if dest.type == "ntrip":
                    current_ver = str(dest.config.get("version", "1.0"))
                    ver = ui.select(
                        NTRIP_VERSIONS,
                        label="NTRIP Version",
                        value=current_ver,
                    ).classes("w-full")
                    config_inputs["version"] = ver

                    username_container = ui.column().classes("w-full")
                    username_container.set_visibility(current_ver == "2.0")
                    with username_container:
                        uname = ui.input(
                            "Username",
                            value=str(dest.config.get("username", "")),
                        ).classes("w-full")
                        config_inputs["username"] = uname

                    def _on_ver_change(e: object) -> None:
                        username_container.set_visibility(ver.value == "2.0")

                    ver.on_value_change(_on_ver_change)

                with ui.row().classes("justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button(
                        "Save",
                        on_click=lambda: _save_edit(dest, config_inputs, dialog),
                    ).props("color=primary")
            dialog.open()

        def _save_edit(
            dest: DestinationProfile,
            config_inputs: dict[str, ui.input | ui.select],
            dialog: ui.dialog,
        ) -> None:
            """Save edited destination."""
            # Validate config fields
            for inp in config_inputs.values():
                if inp.error:
                    ui.notify("Fix validation errors before saving", type="warning")
                    return

            config = {k: v.value for k, v in config_inputs.items() if v.value}
            try:
                updated = dest.model_copy(update={"config": config})
                config_svc.save_destination(updated)
                ui.notify(f"Updated '{dest.name}'", type="positive")
                dialog.close()
                _refresh_list()
            except Exception as exc:
                logger.exception("Failed to update destination")
                ui.notify(f"Error: {exc}", type="negative")

        # Initial render
        _refresh_list()
