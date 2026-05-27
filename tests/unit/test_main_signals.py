"""Tests for sp_rtk_base.main signal handling (Bug C).

uvicorn already handles SIGINT/SIGTERM — we only need to verify that
the SIGHUP handler is installed when ``main()`` runs and that it
forwards to SIGTERM so the existing shutdown path is exercised.
"""

# pyright: reportPrivateUsage=false
# We intentionally exercise the module-private ``_install_sighup_handler``
# from these tests — the helper is a thin signal-registration utility
# that has no public alias to bind a test against.

from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

import pytest

from sp_rtk_base.main import _install_sighup_handler


class TestInstallSighupHandler:
    """The SIGHUP handler installer is a small, well-bounded function."""

    def test_installs_a_handler_on_posix(self) -> None:
        """After calling _install_sighup_handler, SIGHUP has a custom handler.

        We restore the previous handler in a try/finally so the test
        process is left exactly as we found it.
        """
        if not hasattr(signal, "SIGHUP"):
            pytest.skip("SIGHUP not available on this platform")

        previous = signal.getsignal(signal.SIGHUP)
        try:
            _install_sighup_handler()
            current = signal.getsignal(signal.SIGHUP)
            assert callable(current)
            assert current is not previous
            # Must not be the IGN/DFL constants either.
            assert current not in (signal.SIG_DFL, signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGHUP, previous)  # type: ignore[arg-type]

    def test_handler_forwards_to_sigterm(self) -> None:
        """The installed handler re-raises as SIGTERM via os.kill().

        We can't actually let SIGTERM fire during the test (it would
        kill pytest), so we patch os.kill and assert the call shape
        instead.
        """
        if not hasattr(signal, "SIGHUP"):
            pytest.skip("SIGHUP not available on this platform")

        previous = signal.getsignal(signal.SIGHUP)
        try:
            _install_sighup_handler()
            handler = signal.getsignal(signal.SIGHUP)
            assert callable(handler)

            with patch("sp_rtk_base.main.os.kill") as mock_kill:
                # Type-checked frame stand-in is irrelevant for this code.
                handler(signal.SIGHUP, None)  # type: ignore[misc]
            mock_kill.assert_called_once()
            args = mock_kill.call_args.args
            # First arg = pid (positive int), second = SIGTERM.
            assert args[0] > 0
            assert args[1] == signal.SIGTERM
        finally:
            signal.signal(signal.SIGHUP, previous)  # type: ignore[arg-type]


class TestMainInstallsSighupHandler:
    """``main()`` must call ``_install_sighup_handler`` before ``ui.run``.

    If the install happened after uvicorn started, a SIGHUP arriving
    during early startup would still produce a hard exit.
    """

    def test_main_calls_install_sighup_handler(self) -> None:
        """The install happens during main() bootstrap, not lazily."""
        mock_ui = MagicMock()
        mock_nicegui = MagicMock()
        mock_nicegui.ui = mock_ui

        with (
            patch("sp_rtk_base.main.init_app"),
            patch("sp_rtk_base.main._install_sighup_handler") as mock_install,
            patch.dict("sys.modules", {"nicegui": mock_nicegui}),
            patch.dict("os.environ", {}, clear=False),
        ):
            from sp_rtk_base.main import main

            main()

            mock_install.assert_called_once()
