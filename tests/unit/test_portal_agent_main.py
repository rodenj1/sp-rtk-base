"""The ``sp-rtk-base-portal-agent`` process (ticket #83): it listens on its
control socket until told to stop, and nothing else."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from sp_rtk_base.portal_agent.client import AgentUnavailableError, read_status
from sp_rtk_base.portal_agent.main import run
from sp_rtk_base.portal_agent.protocol import SOCKET_ENV


@pytest.fixture()
def sock_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="agent-") as d:
        yield Path(d) / "control.sock"


@pytest.mark.asyncio
async def test_run_serves_status_until_stopped(sock_path: Path) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run(sock_path, stop))
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.01)

    assert (await read_status(sock_path)).state == "not_connected"

    stop.set()
    await asyncio.wait_for(task, 5)
    assert not sock_path.exists()
    with pytest.raises(AgentUnavailableError):
        await read_status(sock_path)


def test_the_agent_process_answers_and_stops_on_sigterm(sock_path: Path) -> None:
    env = {**os.environ, SOCKET_ENV: str(sock_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "sp_rtk_base.portal_agent.main"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 15
        while not sock_path.exists():
            assert proc.poll() is None, proc.stdout.read() if proc.stdout else b""
            assert time.monotonic() < deadline, "agent never listened"
            time.sleep(0.05)

        status = asyncio.run(read_status(sock_path))
        assert status.state == "not_connected"

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.asyncio
async def test_main_listens_where_the_environment_says_and_stops_on_sigterm(
    sock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sp_rtk_base.portal_agent import main as agent_main

    monkeypatch.setenv(SOCKET_ENV, str(sock_path))
    task = asyncio.create_task(agent_main.serve_until_signalled())
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.01)
    assert (await read_status(sock_path)).state == "not_connected"

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 5)
    assert not sock_path.exists()
