"""Tests for the strict net-provision config loader (issue #9).

Deliberately the opposite failure mode from ``ConfigService``: that
service falls back to defaults on a missing/empty/malformed file so a
first boot always has *some* usable config. This loader can't do that
— ``ap_password`` has no default, so there is no valid default config
— so every failure mode here must raise, not degrade.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sp_rtk_base.services.net_provision.config_loader import (
    ENV_CONFIG_PATH,
    NetProvisionConfigError,
    get_net_provision_config_path,
    load_net_provision_config,
)

_VALID_YAML = {"ap_password": "sticker-secret"}


class TestPathResolution:
    def test_explicit_env_var_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_path = tmp_path / "net.yaml"
        monkeypatch.setenv(ENV_CONFIG_PATH, str(env_path))
        assert get_net_provision_config_path() == env_path

    def test_default_path_is_separate_from_the_app_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
        path = get_net_provision_config_path()
        assert path.name == "net_provision.yaml"
        assert "sp-rtk-base" in str(path)


class TestLoadFailsLoudly:
    """Every failure path raises — no silent fallback to defaults."""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.yaml"
        with pytest.raises(NetProvisionConfigError, match="not found"):
            load_net_provision_config(missing)

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "net.yaml"
        path.write_text("")
        with pytest.raises(NetProvisionConfigError):
            load_net_provision_config(path)

    def test_non_mapping_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "net.yaml"
        path.write_text(yaml.dump(["not", "a", "mapping"]))
        with pytest.raises(NetProvisionConfigError, match="mapping"):
            load_net_provision_config(path)

    def test_invalid_yaml_syntax_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "net.yaml"
        path.write_text("ap_password: [unterminated")
        with pytest.raises(NetProvisionConfigError):
            load_net_provision_config(path)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        """ap_password is required — a file without it must not load."""
        path = tmp_path / "net.yaml"
        path.write_text(yaml.dump({"ap_ssid": "custom-ssid"}))
        with pytest.raises(NetProvisionConfigError, match="validation"):
            load_net_provision_config(path)

    def test_out_of_range_field_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "net.yaml"
        path.write_text(yaml.dump({**_VALID_YAML, "boot_wait_seconds": -1}))
        with pytest.raises(NetProvisionConfigError):
            load_net_provision_config(path)


class TestLoadSucceeds:
    def test_valid_minimal_file_loads_with_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "net.yaml"
        path.write_text(yaml.dump(_VALID_YAML))
        config = load_net_provision_config(path)
        assert config.ap_password == "sticker-secret"
        assert config.poll_interval_seconds == 10.0

    def test_valid_full_file_overrides_every_knob(self, tmp_path: Path) -> None:
        path = tmp_path / "net.yaml"
        overrides = {
            "ap_ssid": "field-unit-42",
            "ap_password": "sticker-secret",
            "boot_wait_seconds": 30.0,
            "fallback_window_seconds": 200.0,
            "rescan_interval_seconds": 60.0,
            "poll_interval_seconds": 5.0,
        }
        path.write_text(yaml.dump(overrides))
        config = load_net_provision_config(path)
        assert config.ap_ssid == "field-unit-42"
        assert config.boot_wait_seconds == 30.0
        assert config.fallback_window_seconds == 200.0
        assert config.rescan_interval_seconds == 60.0
        assert config.poll_interval_seconds == 5.0

    def test_uses_resolved_path_when_none_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_path = tmp_path / "net.yaml"
        env_path.write_text(yaml.dump(_VALID_YAML))
        monkeypatch.setenv(ENV_CONFIG_PATH, str(env_path))
        config = load_net_provision_config()
        assert config.ap_password == "sticker-secret"
