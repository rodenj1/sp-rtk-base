"""Tests for sp_rtk_base.services.config_service — YAML config persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sp_rtk_base.models.config_models import (
    AppConfig,
    AppSettings,
    DestinationProfile,
    InputProfile,
)
from sp_rtk_base.services.config_service import ConfigService


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    """Provide a temp config file path."""
    return tmp_path / "sp-rtk-base" / "config.yaml"


@pytest.fixture()
def svc(config_path: Path) -> ConfigService:
    """Create a ConfigService with a temp config path."""
    return ConfigService(config_path=config_path)


def _sample_destination() -> DestinationProfile:
    """Create a sample destination profile for testing."""
    return DestinationProfile(
        name="rtk2go",
        type="ntrip",
        config={
            "caster": "rtk2go.com",
            "mountpoint": "MY_MOUNT",
            "password": "secret",
        },
    )


def _sample_input() -> InputProfile:
    """Create a sample input profile for testing."""
    return InputProfile(
        source="serial",
        config={"port": "/dev/ttyACM0", "baudrate": 115200},
    )


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Tests for ConfigService.load_config()."""

    def test_creates_default_when_missing(self, svc: ConfigService) -> None:
        """load_config creates default config when file doesn't exist."""
        config = svc.load_config()
        assert isinstance(config, AppConfig)
        assert config.input is None
        assert config.destinations == []

    def test_creates_file_on_first_load(
        self, svc: ConfigService, config_path: Path
    ) -> None:
        """load_config creates the YAML file when it doesn't exist."""
        svc.load_config()
        assert config_path.exists()

    def test_creates_parent_directory(
        self, svc: ConfigService, config_path: Path
    ) -> None:
        """load_config creates parent directories."""
        svc.load_config()
        assert config_path.parent.exists()

    def test_loads_existing_config(self, svc: ConfigService, config_path: Path) -> None:
        """load_config reads an existing YAML file."""
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            yaml.dump(
                {
                    "input": {
                        "source": "tcp",
                        "config": {"host": "1.2.3.4", "port": 5015},
                    },
                    "destinations": [],
                    "settings": {"auto_start": True, "status_poll_interval": 3.0},
                }
            ),
            encoding="utf-8",
        )

        config = svc.load_config()
        assert config.input is not None
        assert config.input.source == "tcp"
        assert config.settings.auto_start is True
        assert config.settings.status_poll_interval == 3.0

    def test_handles_empty_file(self, svc: ConfigService, config_path: Path) -> None:
        """load_config returns defaults for an empty file."""
        config_path.parent.mkdir(parents=True)
        config_path.write_text("", encoding="utf-8")

        config = svc.load_config()
        assert config.input is None
        assert config.destinations == []

    def test_handles_invalid_format(
        self, svc: ConfigService, config_path: Path
    ) -> None:
        """load_config returns defaults if file contains non-dict YAML."""
        config_path.parent.mkdir(parents=True)
        config_path.write_text("just a string\n", encoding="utf-8")

        config = svc.load_config()
        assert config.input is None


class TestSaveConfig:
    """Tests for ConfigService.save_config()."""

    def test_saves_to_yaml(self, svc: ConfigService, config_path: Path) -> None:
        """save_config writes a valid YAML file that can be loaded back."""
        config = AppConfig(
            input=_sample_input(),
            destinations=[_sample_destination()],
        )
        svc.save_config(config)

        assert config_path.exists()

        # Verify it's valid YAML by loading back through the service
        svc2 = ConfigService(config_path=config_path)
        loaded = svc2.load_config()
        assert loaded.input is not None
        assert loaded.input.source == "serial"
        assert len(loaded.destinations) == 1

    def test_roundtrip(self, svc: ConfigService) -> None:
        """save_config → load_config roundtrip preserves data."""
        original = AppConfig(
            input=_sample_input(),
            destinations=[_sample_destination()],
            settings=AppSettings(auto_start=True, status_poll_interval=5.0),
        )
        svc.save_config(original)

        loaded = svc.load_config()
        assert loaded.input is not None
        assert loaded.input.source == original.input.source  # type: ignore[union-attr]
        assert len(loaded.destinations) == 1
        assert loaded.destinations[0].name == "rtk2go"
        assert loaded.settings.auto_start is True

    def test_creates_parent_directory(
        self, svc: ConfigService, config_path: Path
    ) -> None:
        """save_config creates parent directories."""
        svc.save_config(AppConfig())
        assert config_path.parent.exists()


class TestGetConfig:
    """Tests for ConfigService.get_config()."""

    def test_loads_on_first_call(self, svc: ConfigService) -> None:
        """get_config loads from disk on first call."""
        config = svc.get_config()
        assert isinstance(config, AppConfig)

    def test_returns_cached_on_second_call(self, svc: ConfigService) -> None:
        """get_config returns cached config on second call."""
        first = svc.get_config()
        second = svc.get_config()
        assert first is second


# ---------------------------------------------------------------------------
# Destination operations
# ---------------------------------------------------------------------------


class TestDestinationOperations:
    """Tests for destination CRUD operations."""

    def test_get_destinations_empty(self, svc: ConfigService) -> None:
        """get_destinations returns empty list initially."""
        assert svc.get_destinations() == []

    def test_save_and_get_destination(self, svc: ConfigService) -> None:
        """save_destination adds a destination retrievable by get_destinations."""
        dest = _sample_destination()
        svc.save_destination(dest)

        destinations = svc.get_destinations()
        assert len(destinations) == 1
        assert destinations[0].name == "rtk2go"

    def test_get_destination_by_name(self, svc: ConfigService) -> None:
        """get_destination finds by name."""
        svc.save_destination(_sample_destination())
        found = svc.get_destination("rtk2go")
        assert found is not None
        assert found.name == "rtk2go"

    def test_get_destination_not_found(self, svc: ConfigService) -> None:
        """get_destination returns None for unknown name."""
        assert svc.get_destination("nonexistent") is None

    def test_save_destination_replaces_existing(self, svc: ConfigService) -> None:
        """save_destination replaces a destination with the same name."""
        dest1 = DestinationProfile(
            name="rtk2go",
            type="ntrip",
            config={
                "caster": "rtk2go.com",
                "mountpoint": "MOUNT_A",
                "password": "pw1",
            },
        )
        dest2 = DestinationProfile(
            name="rtk2go",
            type="ntrip",
            config={
                "caster": "rtk2go.com",
                "mountpoint": "MOUNT_B",
                "password": "pw2",
            },
        )
        svc.save_destination(dest1)
        svc.save_destination(dest2)

        destinations = svc.get_destinations()
        assert len(destinations) == 1
        assert destinations[0].config["mountpoint"] == "MOUNT_B"

    def test_remove_destination(self, svc: ConfigService) -> None:
        """remove_destination removes by name."""
        svc.save_destination(_sample_destination())
        removed = svc.remove_destination("rtk2go")
        assert removed is True
        assert svc.get_destinations() == []

    def test_remove_destination_not_found(self, svc: ConfigService) -> None:
        """remove_destination returns False for unknown name."""
        removed = svc.remove_destination("nonexistent")
        assert removed is False

    def test_multiple_destinations(self, svc: ConfigService) -> None:
        """Multiple destinations can be saved and retrieved."""
        dest1 = _sample_destination()
        dest2 = DestinationProfile(
            name="local-tcp",
            type="tcp_server",
            config={"port": 9000},
        )
        svc.save_destination(dest1)
        svc.save_destination(dest2)

        destinations = svc.get_destinations()
        assert len(destinations) == 2
        names = {d.name for d in destinations}
        assert names == {"rtk2go", "local-tcp"}


# ---------------------------------------------------------------------------
# Input config operations
# ---------------------------------------------------------------------------


class TestInputConfigOperations:
    """Tests for input config operations."""

    def test_get_input_config_none_initially(self, svc: ConfigService) -> None:
        """get_input_config returns None initially."""
        assert svc.get_input_config() is None

    def test_save_and_get_input_config(self, svc: ConfigService) -> None:
        """save_input_config persists input configuration."""
        input_cfg = _sample_input()
        svc.save_input_config(input_cfg)

        loaded = svc.get_input_config()
        assert loaded is not None
        assert loaded.source == "serial"
        assert loaded.config["port"] == "/dev/ttyACM0"


# ---------------------------------------------------------------------------
# Settings operations
# ---------------------------------------------------------------------------


class TestSettingsOperations:
    """Tests for settings operations."""

    def test_get_settings_defaults(self, svc: ConfigService) -> None:
        """get_settings returns defaults initially."""
        settings = svc.get_settings()
        assert settings.auto_start is False
        assert settings.status_poll_interval == 2.0

    def test_save_and_get_settings(self, svc: ConfigService) -> None:
        """save_settings persists settings."""
        svc.save_settings(AppSettings(auto_start=True, status_poll_interval=5.0))

        settings = svc.get_settings()
        assert settings.auto_start is True
        assert settings.status_poll_interval == 5.0


# ---------------------------------------------------------------------------
# Config path resolution
# ---------------------------------------------------------------------------


class TestConfigPath:
    """Tests for config path resolution."""

    def test_explicit_path(self, tmp_path: Path) -> None:
        """ConfigService uses explicit path when provided."""
        path = tmp_path / "custom.yaml"
        svc = ConfigService(config_path=path)
        assert svc.config_path == path

    def test_env_var_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConfigService uses SP_RTK_BASE_CONFIG env var."""
        env_path = tmp_path / "env-config.yaml"
        monkeypatch.setenv("SP_RTK_BASE_CONFIG", str(env_path))

        svc = ConfigService()
        assert svc.config_path == env_path

    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ConfigService uses ~/.config/sp-rtk-base/config.yaml by default."""
        monkeypatch.delenv("SP_RTK_BASE_CONFIG", raising=False)
        svc = ConfigService()
        assert svc.config_path.name == "config.yaml"
        assert "sp-rtk-base" in str(svc.config_path)
