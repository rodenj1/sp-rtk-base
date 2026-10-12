"""``sp-rtk-base-portal-agent``: the Portal agent process.

Run by ``sp-rtk-base-portal-agent.service``. Idle until Claimed: it holds no
base key, contacts nothing and only answers its control socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from sp_rtk_base.portal_agent.control import serve
from sp_rtk_base.portal_agent.protocol import socket_path

logger = logging.getLogger(__name__)


async def run(path: Path, stop: asyncio.Event) -> None:
    """Serve the control socket at ``path`` until ``stop`` is set."""
    server = await serve(path)
    logger.info("Portal agent idle (not Claimed); control socket %s", path)
    try:
        await stop.wait()
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


async def serve_until_signalled() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(socket_path(), stop)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(serve_until_signalled())


if __name__ == "__main__":
    main()
