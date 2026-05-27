"""SP-Base application entry point.

Initializes the FastAPI + NiceGUI application and starts the
uvicorn server on the configured host and port.

Host and port are configurable via environment variables so that e2e
test harnesses (and operators running multiple instances on one box)
can avoid the default ``0.0.0.0:8080`` collision:

- ``SP_RTK_BASE_HOST`` — bind address (default ``0.0.0.0``)
- ``SP_RTK_BASE_PORT`` — TCP port (default ``8080``)
"""

# pyright: reportUnknownMemberType=false
# NiceGUI's ui.run() uses **kwargs: Any, making the function type
# partially unknown. This is a third-party library limitation.

from __future__ import annotations

import os

from sp_rtk_base.app import init_app

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


def main() -> None:
    """Start the SP-Base application server."""
    from nicegui import ui

    init_app()

    host = os.environ.get("SP_RTK_BASE_HOST", DEFAULT_HOST)
    try:
        port = int(os.environ.get("SP_RTK_BASE_PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT

    ui.run(
        title="SP-Base",
        host=host,
        port=port,
        favicon="📡",
        dark=True,
        reload=False,
        show=False,
        storage_secret="sp-rtk-base-dev-secret",
    )


if __name__ == "__main__":
    main()
