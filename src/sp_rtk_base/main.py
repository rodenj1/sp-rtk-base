"""SP-Base application entry point.

Initializes the FastAPI + NiceGUI application and starts the
uvicorn server on the configured host and port.

Host and port are configurable via environment variables so that e2e
test harnesses (and operators running multiple instances on one box)
can avoid the default ``0.0.0.0:8080`` collision:

- ``SP_RTK_BASE_HOST`` — bind address (default ``0.0.0.0``)
- ``SP_RTK_BASE_PORT`` — TCP port (default ``8080``)

Signal handling
---------------

uvicorn already handles ``SIGINT`` and ``SIGTERM`` internally and runs
NiceGUI's ``on_shutdown`` hooks (which in turn calls
:func:`sp_rtk_base.app.shutdown_services`).  We additionally install a
``SIGHUP`` handler — by convention used for "reload" but in our case
treated identically to ``SIGTERM`` — so ``systemctl reload`` or a stray
controlling-terminal hangup also triggers an orderly device-release
and relay-stop instead of a hard exit that leaves BlueZ/serial
handles dangling.
"""

# pyright: reportUnknownMemberType=false
# NiceGUI's ui.run() uses **kwargs: Any, making the function type
# partially unknown. This is a third-party library limitation.

from __future__ import annotations

import logging
import os
import signal

from sp_rtk_base.app import init_app

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080

logger = logging.getLogger(__name__)


def _install_sighup_handler() -> None:
    """Install a SIGHUP handler that triggers an orderly shutdown.

    Re-raises as ``SIGTERM`` so the rest of the shutdown path is the
    same one uvicorn already exercises for ``systemctl stop`` —
    NiceGUI's ``on_shutdown`` hooks fire and
    :func:`sp_rtk_base.app.shutdown_services` releases the GPS device,
    event bridge, and relay engine.

    ``SIGHUP`` is unavailable on Windows; this function silently
    no-ops there.
    """
    if not hasattr(signal, "SIGHUP"):  # pragma: no cover - non-POSIX
        return

    def _handler(signum: int, _frame: object) -> None:
        logger.info("SIGHUP received — initiating graceful shutdown")
        # Forward as SIGTERM so we exercise the same code path
        # uvicorn already wires up for systemd stop / Ctrl-C.
        os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGHUP, _handler)


def main() -> None:
    """Start the SP-Base application server."""
    from nicegui import ui

    init_app()
    _install_sighup_handler()

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
