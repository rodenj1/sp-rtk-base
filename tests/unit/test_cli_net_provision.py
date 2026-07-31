"""Tests for the sp-rtk-base-net-provision console-script entry point.

``main()`` is wiring: load config, build the adapter/state store, run
forever. Everything it delegates to is already unit-tested elsewhere
(config_loader, NmcliAdapter, supervisor), so these tests only check
the wiring itself — mocking every collaborator, the same pattern
``test_main.py``/``test_main_signals.py`` use for the main app's
entry point.
"""

from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

import pytest

from sp_rtk_base.cli.net_provision import _install_shutdown_handler, main
from sp_rtk_base.models.net_provision_models import NetProvisionConfig
from sp_rtk_base.services.net_provision import NetProvisionConfigError

_CONFIG = NetProvisionConfig(ap_password="sticker-secret")


class TestMainWiring:
    def test_main_loads_config_builds_collaborators_and_runs_forever(self) -> None:
        with (
            patch(
                "sp_rtk_base.cli.net_provision.load_net_provision_config",
                return_value=_CONFIG,
            ),
            patch("sp_rtk_base.cli.net_provision.NmcliAdapter") as mock_adapter_cls,
            patch(
                "sp_rtk_base.cli.net_provision.ProvisioningStateStore"
            ) as mock_store_cls,
            patch("sp_rtk_base.cli.net_provision.run_forever") as mock_run_forever,
            patch("sp_rtk_base.cli.net_provision._install_shutdown_handler"),
        ):
            main()

            mock_adapter_cls.assert_called_once_with(_CONFIG)
            mock_store_cls.assert_called_once_with()
            mock_run_forever.assert_called_once()
            kwargs = mock_run_forever.call_args.kwargs
            assert kwargs["adapter"] is mock_adapter_cls.return_value
            assert kwargs["config"] is _CONFIG
            assert kwargs["state_store"] is mock_store_cls.return_value

    def test_main_installs_the_shutdown_handler_before_running(self) -> None:
        with (
            patch(
                "sp_rtk_base.cli.net_provision.load_net_provision_config",
                return_value=_CONFIG,
            ),
            patch("sp_rtk_base.cli.net_provision.NmcliAdapter"),
            patch("sp_rtk_base.cli.net_provision.ProvisioningStateStore"),
            patch("sp_rtk_base.cli.net_provision.run_forever") as mock_run_forever,
            patch(
                "sp_rtk_base.cli.net_provision._install_shutdown_handler"
            ) as mock_install,
        ):
            main()
            mock_install.assert_called_once()
            # Installed before the loop starts, not after it returns.
            assert (
                mock_install.call_args.args[0]
                is (mock_run_forever.call_args.kwargs["stop_event"])
            )

    def test_main_exits_nonzero_when_config_fails_to_load(self) -> None:
        """A missing/invalid config must not fall back to defaults."""
        with (
            patch(
                "sp_rtk_base.cli.net_provision.load_net_provision_config",
                side_effect=NetProvisionConfigError("ap_password required"),
            ),
            patch("sp_rtk_base.cli.net_provision.run_forever") as mock_run_forever,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        mock_run_forever.assert_not_called()


class TestShutdownHandler:
    def test_sigterm_sets_the_stop_event(self) -> None:
        stop_event = MagicMock()
        previous = signal.getsignal(signal.SIGTERM)
        try:
            _install_shutdown_handler(stop_event)
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)  # type: ignore[misc]
            stop_event.set.assert_called_once()
        finally:
            signal.signal(signal.SIGTERM, previous)  # type: ignore[arg-type]

    def test_sigint_sets_the_stop_event(self) -> None:
        stop_event = MagicMock()
        previous = signal.getsignal(signal.SIGINT)
        try:
            _install_shutdown_handler(stop_event)
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)  # type: ignore[misc]
            stop_event.set.assert_called_once()
        finally:
            signal.signal(signal.SIGINT, previous)  # type: ignore[arg-type]
