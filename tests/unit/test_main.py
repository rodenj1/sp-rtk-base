"""Tests for sp_rtk_base.main module — application entry point."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

# Semantic-versioning regex (MAJOR.MINOR.PATCH with optional pre-release /
# build metadata).  Keeps the test agnostic to `cz bump` increments.
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class TestMainModule:
    """Tests for the main module structure."""

    def test_main_function_exists(self) -> None:
        """main function is importable from sp_rtk_base.main."""
        from sp_rtk_base.main import main

        assert callable(main)

    def test_version_importable(self) -> None:
        """Package exposes a valid SemVer ``__version__`` string."""
        from sp_rtk_base import __version__

        assert isinstance(__version__, str)
        assert _SEMVER_RE.match(__version__), (
            f"__version__={__version__!r} is not valid SemVer"
        )


class TestMainEntryPoint:
    """Tests for the main() function execution."""

    def test_main_calls_init_app_and_ui_run(self) -> None:
        """main() calls init_app() then ui.run() with expected args."""
        mock_ui = MagicMock()
        mock_nicegui = MagicMock()
        mock_nicegui.ui = mock_ui

        with (
            patch("sp_rtk_base.main.init_app") as mock_init_app,
            patch.dict("sys.modules", {"nicegui": mock_nicegui}),
        ):
            from sp_rtk_base.main import main

            main()

            mock_init_app.assert_called_once()
            mock_ui.run.assert_called_once()
