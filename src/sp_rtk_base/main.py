"""SP-Base application entry point.

Initializes the FastAPI + NiceGUI application and starts the
uvicorn server on the configured host and port.
"""

# pyright: reportUnknownMemberType=false
# NiceGUI's ui.run() uses **kwargs: Any, making the function type
# partially unknown. This is a third-party library limitation.

from __future__ import annotations

from sp_rtk_base.app import init_app


def main() -> None:
    """Start the SP-Base application server."""
    from nicegui import ui

    init_app()

    ui.run(
        title="SP-Base",
        host="0.0.0.0",
        port=8080,
        favicon="📡",
        dark=True,
        reload=False,
        show=False,
        storage_secret="sp-rtk-base-dev-secret",
    )


if __name__ == "__main__":
    main()
