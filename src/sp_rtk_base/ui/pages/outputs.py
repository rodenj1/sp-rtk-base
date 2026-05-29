"""Outputs page — destination management.

Provides a list of configured destinations with add, edit, delete,
and enable/disable controls.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import logging
import re

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

# Destination name must be safe both as a relay-engine identifier
# AND as a URL path component for DELETE /api/destinations/<name>.
# Slashes break FastAPI routing; spaces and other special characters
# trip the relay's ConfigurationError ("must be alphanumeric").
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _name_validator(value: str | None) -> bool:
    return bool(value and _NAME_RE.fullmatch(value.strip()))


_NAME_VALIDATION_MSG = "Name must be alphanumeric, '_' or '-' (no spaces, no slashes)"


def _run_validators_now(
    inputs: dict[str, ui.input | ui.select],
) -> str | None:
    """Force-run all validators on all inputs and surface the first failure.

    NiceGUI's ``validation`` dict callbacks only fire on user-interaction
    events (``update:model-value``).  An untouched required field
    therefore has ``inp.error = None`` even though its value is empty —
    so the standard ``for inp in inputs.values(): if inp.error: ...``
    pre-save check silently passes and the empty value flows through
    to pydantic, which raises an unfriendly ``ValidationError``.

    This helper explicitly walks each ``validation`` dict and runs the
    callbacks against the current value, setting ``inp.error`` inline
    for visibility AND returning the first error message so callers
    can show a toast.
    """
    for inp in inputs.values():
        validation = getattr(inp, "validation", None)
        if not validation:
            continue
        value = inp.value if inp.value is not None else ""
        for msg, check in validation.items():
            try:
                ok = bool(check(value))
            except Exception:
                ok = False
            if not ok:
                inp.error = msg
                return msg
    return None


# ---------------------------------------------------------------------------
# Type-specific config field definitions
# ---------------------------------------------------------------------------

TYPE_FIELDS: dict[str, list[FieldDef]] = {
    "surepath": [
        # Keys must match ``SurePathProfile`` field names in
        # ``models/config_models.py`` exactly — the relay's
        # SurePathDestinationConfig validates against those.  An
        # earlier version of this map used ``project_id`` / ``token``
        # which produced a confusing pydantic error at relay start.
        ("host", "Host", "", required("Host")),
        ("port", "Port", "50010", port_validation()),
        ("username", "Username", "", required("Username")),
        ("password", "Password", "", required("Password")),
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
        ("port", "Port", "50010"),
        ("username", "Username", ""),
        ("password", "Password", ""),
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
                        # Enable/disable toggle.  ``aria-label`` makes
                        # the switch identifiable to screen readers
                        # (without it the role=switch element had no
                        # accessible name).  ``focus-visible`` keeps
                        # Quasar's default keyboard-focus ring on.
                        ui.switch(
                            value=dest.enabled,
                            on_change=lambda e, n=dest.name: _toggle_enabled(
                                n, e.value
                            ),
                        ).props(f'color=green aria-label="Enable {dest.name}"')

                        # Edit button
                        ui.button(
                            icon="edit",
                            on_click=lambda _e, d=dest: _show_edit_dialog(d),
                        ).props(
                            f"flat round color=blue-4 size=sm "
                            f'aria-label="Edit {dest.name}"'
                        )

                        # Delete button
                        ui.button(
                            icon="delete",
                            on_click=lambda _e, n=dest.name: _confirm_delete(n),
                        ).props(
                            f"flat round color=red-4 size=sm "
                            f'aria-label="Delete {dest.name}"'
                        )

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
                    exc_text = str(exc)
                    # The relay engine raises a "Destination not
                    # found" / "Unknown destination" error when an
                    # operator tries to enable a destination that
                    # was added AFTER the relay was started.  The
                    # config IS saved correctly — the engine just
                    # doesn't know about the new entry until the
                    # next start.  Map this to a clearer message
                    # rather than the raw engine error.
                    if "not found" in exc_text.lower() or "unknown" in exc_text.lower():
                        ui.notify(
                            f"Config saved.  Restart the relay to "
                            f"activate '{name}' (the running engine "
                            "doesn't know about destinations added "
                            "while it's been running).",
                            type="info",
                        )
                    else:
                        ui.notify(
                            f"Config saved but engine error: {exc_text}",
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
                    validation={
                        "Name is required": is_non_empty,
                        _NAME_VALIDATION_MSG: _name_validator,
                    },
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
            name = (name_input.value or "").strip()

            # Force-run name validators (regex + non-empty).  Without
            # this, an untouched/cleared name field passes silently
            # because NiceGUI only marks ``inp.error`` on user-input
            # events.
            name_err = _run_validators_now({"name": name_input})
            if name_err:
                ui.notify(name_err, type="warning")
                return
            if not name:
                # Defensive: regex validator returns False for empty
                # but if validation dict was wiped somewhere we'd
                # still want to reject.
                name_input.error = "Name is required"
                ui.notify("Name is required", type="warning")
                return
            if config_svc.get_destination(name) is not None:
                # Set inline error so the user sees the conflict even
                # if the toast was missed (fixes the "second tab
                # silently does nothing" bug).
                msg = f"'{name}' already exists"
                name_input.error = msg
                ui.notify(msg, type="warning")
                return

            # Force-run validators on every config field.  Catches
            # untouched required fields (e.g. an empty SurePath
            # Username/Password) that would otherwise slip through
            # the post-save pydantic check as a raw HTML 500.
            cfg_err = _run_validators_now(config_inputs)
            if cfg_err:
                ui.notify(cfg_err, type="warning")
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

                # Name field — lets the operator rename without
                # delete + re-add.  If the new name conflicts with
                # another destination, _save_edit catches it and
                # surfaces a clear error.
                name_input = ui.input(
                    "Name",
                    value=dest.name,
                    validation={
                        "Name is required": lambda v: bool(v and v.strip()),
                        _NAME_VALIDATION_MSG: _name_validator,
                    },
                ).classes("w-full")

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
                        on_click=lambda: _save_edit(
                            dest, name_input, config_inputs, dialog
                        ),
                    ).props("color=primary")
            dialog.open()

        def _save_edit(
            dest: DestinationProfile,
            name_input: ui.input,
            config_inputs: dict[str, ui.input | ui.select],
            dialog: ui.dialog,
        ) -> None:
            """Save edited destination."""
            # Force-run validators on every input (same reasoning as
            # _save_new — NiceGUI only fires validators on user-input
            # events, so an untouched required field has no error
            # even when empty).
            cfg_err = _run_validators_now(config_inputs)
            if cfg_err:
                ui.notify(cfg_err, type="warning")
                return
            new_name = (name_input.value or "").strip()
            name_err = _run_validators_now({"name": name_input})
            if name_err:
                ui.notify(name_err, type="warning")
                return
            if not new_name:
                name_input.error = "Name is required"
                ui.notify("Name is required", type="warning")
                return
            # Rename collision check — only fires when name changed.
            if new_name != dest.name:
                existing = {d.name for d in config_svc.get_config().destinations}
                if new_name in existing:
                    ui.notify(
                        f"A destination named '{new_name}' already exists",
                        type="warning",
                    )
                    return

            config = {k: v.value for k, v in config_inputs.items() if v.value}
            try:
                updated = dest.model_copy(update={"name": new_name, "config": config})
                # If the name changed, remove the old entry first so
                # save_destination doesn't end up with both names.
                if new_name != dest.name:
                    config_svc.remove_destination(dest.name)
                config_svc.save_destination(updated)
                ui.notify(f"Updated '{new_name}'", type="positive")
                dialog.close()
                _refresh_list()
            except Exception as exc:
                logger.exception("Failed to update destination")
                ui.notify(f"Error: {exc}", type="negative")

        # Initial render
        _refresh_list()
