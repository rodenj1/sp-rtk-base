"""Settings' **Version & Update** card (variant A of the Update UX prototype).

The card header holds the last-checked line (or "Couldn't check (last
checked …)") and Check now; the SP-Base and Relay rows read
``current → target``, or "Up to date." under them; a note names a newer
release that needs a newer Python. For an Available update, the Release
notes follow in a collapsed expander with SP-Base / Relay tabs, then
**Update to X** (sp-rtk-base#240): disabled with the reason in amber while
it's refused (a Host setup block adds the one-time ``install.sh``
command to copy), and while an Update runs, a thin bar with its phase. The last
Update's outcome is a stripe at the top. When the running version needs
newer Host setup than the host has, a warning shows with the same command.
Python and Platform rows follow.

What it says is decided in ``ui/update_status`` (a covered module). Later
Update work (Release notes, the Update button, Host setup) extends
:func:`version_update_card`.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import platform
import sys

from nicegui import ui

from sp_rtk_base import __version__ as app_version
from sp_rtk_base.services import (
    get_relay_service,
    get_update_check_service,
    get_update_service,
)
from sp_rtk_base.services.update_check import UpdateCheckStatus, running_relay_version
from sp_rtk_base.services.update_service import UpdateRefusedError
from sp_rtk_base.ui.host_setup_status import drift_warning
from sp_rtk_base.ui.update_progress import (
    KIND_COLOURS,
    RELAY_RUNNING_WARNING,
    confirm_text,
    outcome,
    progress_line,
    update_button_text,
)
from sp_rtk_base.ui.update_status import (
    UP_TO_DATE_TEXT,
    NotesTab,
    check_line,
    notes_tabs,
    python_note,
    up_to_date,
    version_rows,
)
from sp_rtk_base.update.host_setup import INSTALL_COMMAND
from sp_rtk_base.update.state import UpdateStatus, Versions

_POLL_S = 1.0
"""How often the card looks for a new check result (an in-memory read)."""

_LABEL_WIDTH = "min-width: 140px"


def version_update_card() -> None:
    """Render the card; it follows the update check while the page is open."""
    service = get_update_check_service()
    update = get_update_service()
    relay_service = get_relay_service()
    relay_version = running_relay_version()
    python_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    shown: list[UpdateCheckStatus | None] = [None]
    shown_update: list[object] = [None]

    async def _check_now() -> None:
        if update.updating():
            return
        check_button.disable()
        render()
        await service.check_now()
        render()

    async def _request(target: Versions) -> None:
        try:
            await update.request(target)
        except UpdateRefusedError as exc:
            ui.notify(exc.message, type="warning")
        await refresh()

    def _confirm(running: Versions, target: Versions) -> None:
        with (
            ui.dialog() as dialog,
            ui.card()
            .classes("q-pa-md")
            .style("min-width: 360px")
            .props('data-testid="update-confirm"'),
        ):
            ui.label(f"{update_button_text(target.app)}?").classes("text-h6 text-white")
            ui.label(confirm_text(running, target)).classes("text-grey-4").props(
                'data-testid="update-confirm-text"'
            )
            if relay_service.is_running:
                with ui.row().classes("items-center no-wrap q-mt-sm"):
                    ui.icon("warning", color="amber")
                    ui.label(RELAY_RUNNING_WARNING).classes("text-amber").props(
                        'data-testid="update-confirm-relay-warning"'
                    )

            async def _go() -> None:
                dialog.close()
                await _request(target)

            with ui.row().classes("w-full justify-end q-mt-md"):
                ui.button("Cancel", on_click=dialog.close).props(
                    'flat data-testid="update-confirm-cancel"'
                )
                ui.button("Update", icon="system_update", on_click=_go).props(
                    'color=primary data-testid="update-confirm-go"'
                )
        dialog.open()

    with (
        ui.card()
        .classes("w-full q-pa-md q-mt-md")
        .props('data-testid="version-update-card"')
    ):
        with ui.row().classes("w-full items-center"):
            ui.label("Version & Update").classes("text-h6 text-white")
            ui.space()
            with ui.row().classes("items-center q-gutter-sm"):
                check_area = ui.row().classes("items-center q-gutter-xs")
                check_button = ui.button(
                    "Check now", icon="refresh", on_click=_check_now
                ).props('flat dense size=sm data-testid="update-check-now"')
        ui.separator()
        outcome_area = ui.column().classes("w-full q-gutter-none")
        body = ui.column().classes("w-full q-gutter-none")
        update_area = ui.column().classes("w-full q-gutter-none q-mt-sm")
        for label, value in (
            ("Python", python_version),
            ("Platform", platform.platform()),
        ):
            with ui.row().classes("w-full items-center q-py-xs"):
                ui.label(label).classes("text-grey-4").style(_LABEL_WIDTH)
                ui.label(value).classes("text-white text-weight-medium")

    def render() -> None:
        status = service.status
        if status is shown[0]:
            return
        shown[0] = status

        line = check_line(status)
        check_area.clear()
        with check_area:
            if line.checking:
                ui.spinner(size="xs")
            elif line.failed:
                ui.icon("cloud_off", color="amber").classes("text-caption")
            colour = "text-amber" if line.failed else "text-grey-6"
            ui.label(line.text).classes(f"{colour} text-caption").props(
                'data-testid="update-check-line"'
            )
        check_button.set_enabled(not status.checking and not update.updating())

        body.clear()
        with body:
            for row in version_rows(status, app_version, relay_version):
                with (
                    ui.row()
                    .classes("w-full items-center q-py-xs")
                    .props(
                        f'data-testid="version-row-{row.label.lower().replace(" ", "-")}"'
                    )
                ):
                    ui.label(row.label).classes("text-grey-4").style(_LABEL_WIDTH)
                    ui.label(row.current).classes("text-white text-weight-medium")
                    if row.target is not None:
                        ui.icon("arrow_forward", color="teal").classes("text-caption")
                        ui.label(row.target).classes("text-teal text-weight-medium")
            if up_to_date(status):
                ui.label(UP_TO_DATE_TEXT).classes("text-grey-5 q-mt-xs")
            note = python_note(status)
            if note is not None:
                ui.label(note).classes("text-caption text-grey-5").props(
                    'data-testid="update-python-note"'
                )
            tabs = notes_tabs(status)
            if tabs is not None:
                _release_notes(tabs)

    async def render_update() -> None:
        """The outcome stripe, the step bar, and Update with its refusal."""
        check = service.status
        last = check.last_good
        status = update.status()
        updating = status is not None and not status.finished
        available = last is not None and last.available
        host = await update.host_setup()
        refusal = await update.refusal(host) if available and not updating else None
        command = refusal.command if refusal is not None else None
        drift = drift_warning(host, command_shown=command is not None)
        key = (check, status, refusal.code if refusal is not None else None, drift)
        if key == shown_update[0]:
            return
        shown_update[0] = key
        check_button.set_enabled(not check.checking and not updating)

        _outcome_stripe(outcome_area, status)
        update_area.clear()
        with update_area:
            line = progress_line(status)
            if line is not None:
                ui.linear_progress(value=line.value, show_value=False).props(
                    "color=orange"
                )
                ui.label(line.text).classes("text-grey-4 text-caption").props(
                    'data-testid="update-progress"'
                )
            if drift is not None:
                ui.label(drift).classes("text-warning text-caption").props(
                    'data-testid="update-drift"'
                )
                _install_command("update-drift-command")
            if last is None or not available:
                return
            if refusal is not None and refusal.code != "updating":
                ui.label(refusal.message).classes("text-warning text-caption").props(
                    'data-testid="update-refusal"'
                )
                if command is not None:
                    _install_command("update-refusal-command")
            running = Versions(app=last.running_app, relay=last.running_relay)
            target = Versions(app=last.target.app, relay=last.target.relay)
            ui.button(
                update_button_text(target.app),
                icon="system_update",
                on_click=lambda: _confirm(running, target),
            ).props('color=primary data-testid="update-button"').set_enabled(
                refusal is None and not updating and not check.checking
            )

    async def refresh() -> None:
        render()
        await render_update()

    render()
    ui.timer(0.1, render_update, once=True)
    ui.timer(_POLL_S, refresh)


def _install_command(test_id: str) -> None:
    """The one-time ``install.sh`` re-run, in a block with a copy button."""
    ui.code(INSTALL_COMMAND, language="bash").classes("w-full q-my-xs").props(
        f'data-testid="{test_id}"'
    )


def _outcome_stripe(area: ui.column, status: UpdateStatus | None) -> None:
    """The last Update's outcome, at the top of the card."""
    area.clear()
    shown = outcome(status)
    if shown is None:
        return
    with (
        area,
        ui.row()
        .classes("w-full items-start q-pa-sm rounded-borders q-my-sm no-wrap")
        .style(
            f"border-left: 4px solid {KIND_COLOURS[shown.kind]}; background: #20203a"
        )
        .props('data-testid="update-outcome"'),
    ):
        ui.label(shown.text).classes("text-grey-3")


def _slug(label: str) -> str:
    return label.lower().replace(" ", "-")


def _release_notes(tabs: list[NotesTab]) -> None:
    """The collapsed **Release notes** expander: one tab per package.

    The notes' HTML comes from ``notes_html``, which has already escaped
    any raw HTML in them; ``ui.html`` sanitises it once more.
    """
    with (
        ui.expansion("Release notes", icon="notes")
        .classes("w-full q-mt-sm")
        .props('data-testid="release-notes"')
    ):
        with ui.tabs().classes("text-white") as tab_bar:
            handles = [
                ui.tab(tab.label).props(
                    f'data-testid="release-notes-tab-{_slug(tab.label)}"'
                )
                for tab in tabs
            ]
        with ui.tab_panels(tab_bar, value=handles[0]).classes("w-full bg-transparent"):
            for handle, tab in zip(handles, tabs, strict=True):
                with ui.tab_panel(handle).props(
                    f'data-testid="release-notes-{_slug(tab.label)}"'
                ):
                    if tab.text is not None:
                        ui.label(tab.text).classes("text-grey-5 text-italic")
                    for release in tab.releases:
                        ui.label(release.heading).classes(
                            "text-subtitle2 text-white q-mt-sm"
                        )
                        if release.includes is not None:
                            ui.label(release.includes).classes(
                                "text-caption text-grey-6"
                            )
                        if release.html is not None:
                            ui.html(release.html).classes(
                                "nicegui-markdown text-grey-3"
                            )
                        else:
                            ui.label(release.text or "").classes(
                                "text-grey-6 text-italic"
                            )
                        for pre in release.pre_releases:
                            ui.label(pre.heading).classes(
                                "text-caption text-grey-5 q-mt-xs"
                            )
                            ui.html(pre.html).classes("nicegui-markdown text-grey-4")
