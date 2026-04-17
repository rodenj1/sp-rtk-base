"""Shared page layout with navigation for SP-Base UI.

Provides a consistent header, left drawer navigation, and content area
across all pages. Optimized for mobile/tablet with responsive behavior.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from nicegui import ui

from sp_base import __version__ as app_version


# Navigation structure: list of (section_header | None, label, path, icon)
# A None section_header means "no header before this item".
NAVIGATION_SECTIONS: list[tuple[str | None, list[tuple[str, str, str]]]] = [
    (None, [
        ("Dashboard", "/", "dashboard"),
    ]),
    ("Configuration", [
        ("Input", "/input", "input"),
        ("Outputs", "/outputs", "output"),
    ]),
    ("Survey", [
        ("Survey-In", "/survey", "explore"),
    ]),
    ("System", [
        ("Settings", "/settings", "settings"),
        ("Advanced GPS", "/gps-config", "memory"),
    ]),
]
"""Navigation menu sections with grouped items."""


@contextmanager
def page_layout(title: str) -> Iterator[None]:
    """Context manager that wraps page content with the shared layout.

    Provides a responsive header with navigation drawer toggle,
    a left drawer with navigation links (overlay on mobile, push on desktop),
    and yields for page content.

    Args:
        title: The page title shown in the header.
    """
    ui.dark_mode(True)

    # Add viewport meta for mobile
    ui.add_head_html(
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'maximum-scale=1, user-scalable=no">'
    )

    # Responsive CSS overrides
    ui.add_head_html("""
    <style>
    /* Touch-friendly: min 44px tap targets */
    .q-btn { min-height: 44px; min-width: 44px; }
    .q-field { min-height: 48px; }

    /* Mobile: stack cards vertically, full width */
    @media (max-width: 768px) {
        .sp-metric-row { flex-direction: column !important; }
        .sp-metric-row > * { width: 100% !important; flex: none !important; }
        .text-h4 { font-size: 1.5rem !important; }
        .q-dialog__inner > .q-card { width: 95vw !important; max-width: 95vw !important; }
    }

    /* Tablet: 2 columns */
    @media (min-width: 769px) and (max-width: 1024px) {
        .sp-metric-row { flex-wrap: wrap !important; }
        .sp-metric-row > * { flex: 1 1 45% !important; }
    }
    </style>
    """)

    with ui.header(elevated=True).classes(
        "items-center justify-between q-px-md"
    ).style("background-color: #1a1a2e"):
        ui.button(
            on_click=lambda: left_drawer.toggle(), icon="menu"
        ).props("flat color=white round")
        ui.label("SP-Base").classes("text-h6 text-white q-ml-sm")
        ui.space()
        ui.label(title).classes("text-subtitle1 text-white")

    with ui.left_drawer(
        top_corner=True,
        bottom_corner=True,
        value=False,  # Closed by default (mobile-first)
        elevated=True,
    ).classes("q-pa-md").style(
        "background-color: #16213e"
    ).props(
        'breakpoint=1024 behavior=mobile overlay'
    ) as left_drawer:
        for section_header, items in NAVIGATION_SECTIONS:
            if section_header is not None:
                ui.separator().classes("q-my-sm")
                ui.label(section_header).classes(
                    "text-overline text-grey-6 q-mb-xs q-ml-sm"
                )
            for label, path, icon in items:
                _nav_link(label, path, icon, left_drawer)

    with ui.column().classes("w-full q-pa-md"):
        yield

    # Footer with version
    with ui.footer().classes("items-center justify-end q-px-md").style(
        "background-color: #0f0f1e; height: 32px;"
    ):
        ui.label(f"SP-Base v{app_version}").classes(
            "text-caption text-grey-6"
        )


def _nav_link(label: str, path: str, icon: str, drawer: ui.left_drawer) -> None:
    """Create a navigation link in the drawer.

    Closes the drawer on click for mobile UX.

    Args:
        label: Display text for the link.
        path: Route path to navigate to.
        icon: Material icon name.
        drawer: The drawer to close on mobile after navigation.
    """
    with ui.link(target=path).classes("no-underline w-full"):
        with ui.row().classes(
            "items-center q-pa-sm rounded-borders w-full"
        ).style("cursor: pointer; min-height: 48px"):
            ui.icon(icon).classes("text-grey-4")
            ui.label(label).classes("text-grey-2")
