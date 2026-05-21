"""Settings page — application configuration.

Provides controls for application-level settings such as
auto-start, metrics, and status poll interval.
Input source configuration has moved to the dedicated Input page.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import logging
import platform
import sys

from nicegui import ui

from sp_rtk_base import __version__ as app_version
from sp_rtk_base.models.config_models import AppSettings
from sp_rtk_base.services import get_config_service
from sp_rtk_base.ui.layout import page_layout

logger = logging.getLogger(__name__)


@ui.page("/settings")
def settings_page() -> None:
    """Render the settings page."""
    config_svc = get_config_service()

    with page_layout("Settings"):
        ui.label("Settings").classes("text-h4 text-white q-mb-md")

        # ---- Application Settings Section ----
        with ui.card().classes("w-full q-pa-md"):
            ui.label("Application Settings").classes("text-h6 text-white")
            ui.separator()

            current_settings = config_svc.get_settings()

            auto_start = ui.switch(
                "Auto-start relay on application launch",
                value=current_settings.auto_start,
            ).classes("q-mt-sm")

            metrics_enabled = ui.switch(
                "Enable Prometheus metrics endpoint (/metrics)",
                value=current_settings.metrics_enabled,
            ).classes("q-mt-sm")

            poll_interval = ui.number(
                "Status poll interval (seconds)",
                value=current_settings.status_poll_interval,
                min=0.5,
                max=30.0,
                step=0.5,
            ).classes("w-full q-mt-sm")

            def _save_settings() -> None:
                """Save application settings."""
                try:
                    interval = float(poll_interval.value or 2.0)
                    if interval < 0.5 or interval > 30.0:
                        ui.notify(
                            "Poll interval must be between 0.5 and 30 seconds",
                            type="warning",
                        )
                        return

                    settings = AppSettings(
                        auto_start=bool(auto_start.value),
                        status_poll_interval=interval,
                        metrics_enabled=bool(metrics_enabled.value),
                    )
                    config_svc.save_settings(settings)
                    ui.notify("Settings saved", type="positive")
                except Exception as exc:
                    logger.exception("Failed to save settings")
                    ui.notify(f"Error saving settings: {exc}", type="negative")

            ui.button("Save Settings", icon="save", on_click=_save_settings).props(
                "color=primary"
            ).classes("q-mt-md")

        # ---- Version Information Section ----
        with ui.card().classes("w-full q-pa-md q-mt-md"):
            ui.label("Version Information").classes("text-h6 text-white")
            ui.separator()

            # Get relay version
            try:
                from sp_rtk_base_relay import __version__ as relay_version
            except ImportError:
                relay_version = "not installed"

            version_rows: list[tuple[str, str]] = [
                ("SP-Base", app_version),
                ("SP-Base Relay", relay_version),
                (
                    "Python",
                    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                ),
                ("Platform", platform.platform()),
            ]

            for label, version in version_rows:
                with ui.row().classes("w-full items-center q-py-xs"):
                    ui.label(label).classes("text-grey-4").style("min-width: 140px")
                    ui.label(version).classes("text-white text-weight-medium")
