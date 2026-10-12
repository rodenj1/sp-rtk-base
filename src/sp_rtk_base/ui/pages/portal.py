"""Portal page: this base's connection to the Portal (spec #82).

Reads the Portal agent's status over its control socket. Not to be
confused with the setup-AP captive portal (``services/net_provision``).
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

from nicegui import ui

from sp_rtk_base.portal_agent.protocol import socket_path
from sp_rtk_base.ui.layout import page_layout
from sp_rtk_base.ui.portal_status import PortalView, portal_view

_POLL_S = 5.0
"""How often the page asks the agent again (a local socket round trip)."""


@ui.page("/portal", dark=True)
async def portal_page() -> None:
    """Render the Portal status card."""
    with page_layout("Portal"):
        ui.label("Portal").classes("text-h4 text-white q-mb-md")
        card = ui.card().classes("w-full q-pa-md")
        shown: list[PortalView | None] = [None]

        def render(view: PortalView) -> None:
            if view == shown[0]:
                return
            shown[0] = view
            card.clear()
            with card:
                ui.label(view.headline).classes("text-h6 text-white").props(
                    'data-testid="portal-headline"'
                )
                if view.detail:
                    ui.label(view.detail).classes("text-grey-5").props(
                        'data-testid="portal-detail"'
                    )
                if view.can_connect:
                    # Wired up by the Claim ticket (spec #82).
                    ui.button("Connect to Portal", icon="cloud").props(
                        'color=primary disable data-testid="portal-connect"'
                    )

        async def refresh() -> None:
            render(await portal_view(socket_path()))

        await refresh()
        ui.timer(_POLL_S, refresh)
