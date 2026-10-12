"""Portal page: this base's connection to the Portal (spec #82).

Reads the Portal agent's status over its control socket. Not to be
confused with the setup-AP captive portal (``services/net_provision``).
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

from datetime import datetime, timezone

from nicegui import ui

from sp_rtk_base.portal_agent.protocol import socket_path
from sp_rtk_base.ui.layout import page_layout
from sp_rtk_base.ui.portal_status import PortalView, connect, countdown, portal_view

_POLL_S = 5.0
"""How often the page asks the agent again (a local socket round trip)."""

_PAIRING_POLL_S = 1.0
"""While pairing, ask every second so Connected shows as soon as it's true."""


@ui.page("/portal", dark=True)
async def portal_page() -> None:
    """Render the Portal status card."""
    with page_layout("Portal"):
        ui.label("Portal").classes("text-h4 text-white q-mb-md")
        card = ui.card().classes("w-full q-pa-md")
        shown: list[PortalView | None] = [None]
        clock: list[ui.label | None] = [None]

        async def on_connect(button: ui.button) -> None:
            button.props("loading")
            problem = await connect(socket_path())
            button.props(remove="loading")
            if problem:
                ui.notify(problem, type="negative")
            await refresh()

        def tick() -> None:
            view = shown[0]
            if clock[0] is not None and view and view.expires_at:
                clock[0].set_text(
                    countdown(view.expires_at, datetime.now(timezone.utc))
                )

        def render(view: PortalView) -> None:
            if view == shown[0]:
                return
            shown[0] = view
            clock[0] = None
            card.clear()
            with card:
                ui.label(view.headline).classes("text-h6 text-white").props(
                    'data-testid="portal-headline"'
                )
                if view.code:
                    with ui.row().classes("items-baseline q-gutter-md"):
                        ui.label(view.code).classes(
                            "text-h3 text-white text-weight-bold"
                        ).style("letter-spacing: 0.1em; font-family: monospace").props(
                            'data-testid="portal-code"'
                        )
                        clock[0] = (
                            ui.label("")
                            .classes("text-h6 text-grey-4")
                            .props('data-testid="portal-countdown"')
                        )
                    tick()
                if view.detail:
                    ui.label(view.detail).classes("text-grey-5").props(
                        'data-testid="portal-detail"'
                    )
                if view.approve_url:
                    approve_url = view.approve_url
                    ui.button(
                        "Approve on Portal",
                        icon="open_in_new",
                        on_click=lambda: ui.navigate.to(approve_url, new_tab=True),
                    ).props('color=primary data-testid="portal-approve"')
                if view.facts:
                    with ui.grid(columns="auto 1fr").classes("q-gutter-x-md"):
                        for label, value in view.facts:
                            ui.label(label).classes("text-grey-5")
                            slug = label.lower().replace(" ", "-")
                            ui.label(value).classes("text-white").props(
                                f'data-testid="portal-{slug}"'
                            )
                if view.can_connect:
                    connect_button = ui.button("Connect to Portal", icon="cloud").props(
                        'color=primary data-testid="portal-connect"'
                    )
                    connect_button.on_click(lambda: on_connect(connect_button))

        async def refresh() -> None:
            render(await portal_view(socket_path()))

        await refresh()
        ui.timer(_POLL_S, refresh)
        ui.timer(
            _PAIRING_POLL_S,
            lambda: refresh() if shown[0] and shown[0].code else None,
        )
        ui.timer(1.0, tick)
