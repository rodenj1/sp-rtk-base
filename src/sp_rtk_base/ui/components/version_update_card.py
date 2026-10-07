"""Settings' **Version & Update** card (variant A of the Update UX prototype).

The card header holds the last-checked line (or "Couldn't check (last
checked …)") and Check now; the SP-Base and Relay rows read
``current → target``, or "Up to date." under them; a note names a newer
release that needs a newer Python. For an Available update, the Release
notes follow in a collapsed expander with SP-Base / Relay tabs. Python and
Platform rows follow.

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
from sp_rtk_base.services import get_update_check_service
from sp_rtk_base.services.update_check import UpdateCheckStatus, running_relay_version
from sp_rtk_base.ui.update_status import (
    UP_TO_DATE_TEXT,
    NotesTab,
    check_line,
    notes_tabs,
    python_note,
    up_to_date,
    version_rows,
)

_POLL_S = 1.0
"""How often the card looks for a new check result (an in-memory read)."""

_LABEL_WIDTH = "min-width: 140px"


def version_update_card() -> None:
    """Render the card; it follows the update check while the page is open."""
    service = get_update_check_service()
    relay_version = running_relay_version()
    python_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    shown: list[UpdateCheckStatus | None] = [None]

    async def _check_now() -> None:
        check_button.disable()
        render()
        await service.check_now()
        render()

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
        body = ui.column().classes("w-full q-gutter-none")
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
        check_button.set_enabled(not status.checking)

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

    render()
    ui.timer(_POLL_S, render)


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
