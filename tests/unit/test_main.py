"""Tests for sp_base.main module — application entry point."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestMainModule:
    """Tests for the main module structure."""

    def test_main_function_exists(self) -> None:
        """main function is importable from sp_base.main."""
        from sp_base.main import main

        assert callable(main)

    def test_version_importable(self) -> None:
        """Package version is importable."""
        from sp_base import __version__

        assert isinstance(__version__, str)
        assert __version__ == "0.1.0"


class TestMainEntryPoint:
    """Tests for the main() function execution."""

    def test_main_calls_init_app_and_ui_run(self) -> None:
        """main() calls init_app() then ui.run() with expected args."""
        mock_ui = MagicMock()
        mock_nicegui = MagicMock()
        mock_nicegui.ui = mock_ui

        with (
            patch("sp_base.main.init_app") as mock_init_app,
            patch.dict("sys.modules", {"nicegui": mock_nicegui}),
        ):
            from sp_base.main import main

            main()

            mock_init_app.assert_called_once()
            mock_ui.run.assert_called_once()
