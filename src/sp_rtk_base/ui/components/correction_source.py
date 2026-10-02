"""The New / Edit Correction source dialog on the Survey page (issue #192).

An NTRIP form: name, caster, port, mountpoint, username and a write-only
password, the NTRIP version and TLS (v2 only). The timeouts and retries are
the Relay's own defaults, shown read-only. A saved password is never shown
back: leaving the field blank keeps it, and "Remove saved password" clears it.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable

from nicegui import ui
from pydantic import ValidationError
from sp_rtk_base_relay.config import NtripInputConfig

from sp_rtk_base.models.config_models import (
    CORRECTION_SOURCE_NAME_PATTERN,
    CorrectionSourceProfile,
    NtripCorrectionConfig,
)
from sp_rtk_base.services.config_service import (
    ConfigService,
    CorrectionSourceExistsError,
    CorrectionSourceNotFoundError,
)

_NAME_RE = re.compile(CORRECTION_SOURCE_NAME_PATTERN)
_NAME_MSG = "Letters, digits, '-' and '_' only"

# The Relay's defaults for what isn't stored, as the dialog shows them.
_ADVANCED = (
    ("connection_timeout", "Connection timeout", "s"),
    ("data_timeout", "Data timeout", "s"),
    ("retry_initial_delay", "First retry after", "s"),
    ("retry_max_delay", "Retries back off to", "s"),
    ("retry_multiplier", "Back-off multiplier", "times"),
)


def _relay_default(field_name: str) -> object:
    for field in dataclasses.fields(NtripInputConfig):
        if field.name == field_name:
            return field.default
    return "—"


def _first_error(exc: ValidationError) -> str:
    """The first problem, without the values entered (one may be the password)."""
    errors = exc.errors()
    if not errors:
        return "Invalid Correction source"
    field = ".".join(str(p) for p in errors[0].get("loc", ()))
    msg = str(errors[0].get("msg", "invalid"))
    return f"{field}: {msg}" if field else msg


def correction_source_dialog(
    config_svc: ConfigService,
    existing: CorrectionSourceProfile | None,
    on_saved: Callable[[str], None],
    on_deleted: Callable[[str], None],
) -> None:
    """Open the dialog: a new source when ``existing`` is None, else an edit."""
    cfg = existing.config if existing else None
    has_password = bool(cfg and cfg.password)
    remove_password = False

    with (
        ui.dialog() as dlg,
        ui.card().classes("q-pa-md").style("min-width: 420px"),
    ):
        title = "Edit Correction source" if existing else "New Correction source"
        ui.label(title).classes("text-h6 text-white")
        ui.separator()

        name = ui.input(
            "Name",
            value=existing.name if existing else "",
            validation={_NAME_MSG: lambda v: bool(_NAME_RE.match(v or ""))},
        ).classes("w-full")
        caster = ui.input(
            "Caster",
            value=cfg.caster if cfg else "",
            validation={"Caster is required": lambda v: bool((v or "").strip())},
        ).classes("w-full")
        port = ui.number(
            "Port", value=cfg.port if cfg else 2101, min=1, max=65535, step=1
        ).classes("w-full")
        mountpoint = ui.input(
            "Mountpoint",
            value=cfg.mountpoint if cfg else "",
            validation={"Mountpoint is required": lambda v: bool((v or "").strip())},
        ).classes("w-full")
        username = ui.input("Username", value=cfg.username if cfg else "").classes(
            "w-full"
        )
        username.props('hint="Leave blank for an anonymous caster"')
        password = ui.input(
            "Password",
            password=True,
            password_toggle_button=True,
            placeholder="•••••• (saved — leave blank to keep)" if has_password else "",
        ).classes("w-full")

        with ui.row().classes("items-center gap-2"):
            removed_label = ui.label("The saved password will be removed").classes(
                "text-caption text-warning"
            )
            removed_label.set_visibility(False)
            if has_password:

                def _remove_password() -> None:
                    nonlocal remove_password
                    remove_password = True
                    password.value = ""
                    password.props('placeholder=""')
                    removed_label.set_visibility(True)

                ui.button(
                    "Remove saved password", icon="key_off", on_click=_remove_password
                ).props("flat dense color=warning")

        with ui.row().classes("w-full items-center gap-4"):
            version = ui.select(
                ["1.0", "2.0"],
                label="NTRIP version",
                value=cfg.version if cfg else "2.0",
            ).classes("col-grow")
            tls = ui.switch("TLS", value=cfg.tls if cfg else False)

        def _sync_tls() -> None:
            is_v2 = version.value == "2.0"
            tls.set_enabled(is_v2)
            if not is_v2:
                tls.value = False

        version.on_value_change(lambda _: _sync_tls())
        _sync_tls()

        with ui.expansion("Advanced (Relay defaults)").classes("w-full"):
            for field_name, label, unit in _ADVANCED:
                ui.label(f"{label}: {_relay_default(field_name)} {unit}").classes(
                    "text-caption text-grey-4"
                )

        def _save() -> None:
            for field in (name, caster, mountpoint):
                field.validate()
            if not (name.value and _NAME_RE.match(name.value)):
                ui.notify(_NAME_MSG, type="warning")
                return
            fields = {
                "caster": (caster.value or "").strip(),
                "port": int(port.value or 2101),
                "mountpoint": (mountpoint.value or "").strip(),
                "username": (username.value or "").strip(),
                "version": version.value,
                "tls": bool(tls.value),
            }
            new_password = password.value or ""
            try:
                if existing is None:
                    source = CorrectionSourceProfile(
                        name=name.value,
                        config=NtripCorrectionConfig.model_validate(
                            {**fields, "password": new_password}
                        ),
                    )
                    config_svc.create_correction_source(source)
                else:
                    source = config_svc.update_correction_source(
                        existing.name,
                        new_name=name.value,
                        changes=fields,
                        password=new_password,
                        remove_password=remove_password,
                    )
            except CorrectionSourceExistsError as exc:
                name.error = "Name already used"
                ui.notify(str(exc), type="warning")
                return
            except ValidationError as exc:
                ui.notify(_first_error(exc), type="warning")
                return
            except CorrectionSourceNotFoundError as exc:
                ui.notify(str(exc), type="negative")
                return
            ui.notify(f"Saved '{source.name}'", type="positive")
            dlg.close()
            on_saved(source.name)

        def _delete() -> None:
            assert existing is not None
            config_svc.remove_correction_source(existing.name)
            ui.notify(f"Deleted '{existing.name}'", type="info")
            dlg.close()
            on_deleted(existing.name)

        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            if existing is not None:
                ui.button("Delete", icon="delete", on_click=_delete).props(
                    "flat color=negative"
                )
            ui.button("Cancel", on_click=dlg.close).props("flat")
            ui.button("Save", on_click=_save).props("color=primary")
    dlg.open()
