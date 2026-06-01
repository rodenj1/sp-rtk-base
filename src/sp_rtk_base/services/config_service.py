"""YAML-based configuration persistence service.

Manages loading, saving, and manipulating the application configuration
file at ``~/.config/sp-rtk-base/config.yaml`` (or ``SP_RTK_BASE_CONFIG`` env override).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from sp_rtk_base.models.config_models import (
    AppConfig,
    AppSettings,
    BaseStationPosition,
    DestinationProfile,
    DeviceProfile,
    InputProfile,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "sp-rtk-base"
DEFAULT_CONFIG_FILENAME = "config.yaml"
ENV_CONFIG_PATH = "SP_RTK_BASE_CONFIG"


def _filter_invalid_base_positions(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``base_positions`` entries that fail individual validation.

    v0.3.17 added a regex constraint (``^[A-Za-z0-9_-]+$``) plus length
    bounds to :class:`BaseStationPosition.name`.  Without this filter,
    a YAML written by an earlier version with a non-conforming legacy
    name (spaces, slashes, etc.) makes ``AppConfig.model_validate``
    raise, which bricks the whole app — every endpoint that calls
    ``get_config()`` returns HTTP 500.

    Keep the model-layer regex (still rejects bad input at POST time
    with 422), but be lenient on *load*: drop unparseable entries
    individually with a warning so the operator sees what was skipped.
    The on-disk YAML is left untouched — the user keeps their data
    and can rename via the UI to restore.
    """
    raw: Any = data.get("base_positions")
    if not isinstance(raw, list):
        return data
    kept: list[Any] = []
    for idx, item in enumerate(raw):  # type: ignore[arg-type]
        try:
            BaseStationPosition.model_validate(item)
        except ValidationError as exc:
            name: str = "<malformed>"
            if isinstance(item, dict):
                raw_name = item.get("name", "<unknown>")  # type: ignore[arg-type]
                if isinstance(raw_name, str):
                    name = raw_name
            errs = exc.errors()
            msg = errs[0].get("msg", "validation error") if errs else "validation error"
            logger.warning(
                "Dropping base_positions[%d] name=%r during load — %s. "
                "Rename or remove it in the YAML (or via the UI) to restore.",
                idx,
                name,
                msg,
            )
            continue
        kept.append(item)
    if len(kept) == len(raw):  # type: ignore[arg-type]
        return data
    cleaned: dict[str, Any] = dict(data)
    cleaned["base_positions"] = kept
    return cleaned


def _get_config_path() -> Path:
    """Resolve the configuration file path.

    Checks the ``SP_RTK_BASE_CONFIG`` environment variable first,
    then falls back to ``~/.config/sp-rtk-base/config.yaml``.

    Returns:
        Resolved path to the configuration file.
    """
    env_path = os.environ.get(ENV_CONFIG_PATH)
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILENAME


class ConfigService:
    """YAML-based application configuration persistence.

    Provides load/save operations for the application configuration,
    including input source settings, destination profiles, and
    application-level preferences.

    Args:
        config_path: Optional explicit path to the config file.
            If not provided, uses ``SP_RTK_BASE_CONFIG`` env var or
            the default ``~/.config/sp-rtk-base/config.yaml``.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._config_path = config_path or _get_config_path()
        self._config: AppConfig | None = None

    @property
    def config_path(self) -> Path:
        """The resolved configuration file path."""
        return self._config_path

    # ------------------------------------------------------------------
    # Full config load / save
    # ------------------------------------------------------------------

    def load_config(self) -> AppConfig:
        """Load configuration from YAML file.

        Creates a default config if the file does not exist.

        Returns:
            The loaded (or default) application configuration.
        """
        if not self._config_path.exists():
            logger.info(
                "Config file not found at %s — creating default", self._config_path
            )
            self._config = AppConfig()
            self.save_config(self._config)
            return self._config

        raw_text = self._config_path.read_text(encoding="utf-8")
        if not raw_text.strip():
            logger.warning("Config file is empty — using defaults")
            self._config = AppConfig()
            return self._config

        data = yaml.safe_load(raw_text)
        if not isinstance(data, dict):
            logger.warning("Config file has invalid format — using defaults")
            self._config = AppConfig()
            return self._config

        # data is dict[Unknown, Unknown] after the isinstance narrow above;
        # YAML keys at this layer are always strings (config schema), so
        # the assignment annotation is sound but pyright can't infer it.
        data_dict: dict[str, Any] = data  # pyright: ignore[reportUnknownVariableType, reportAssignmentType]
        data_dict = _filter_invalid_base_positions(data_dict)
        self._config = AppConfig.model_validate(data_dict)
        logger.info("Loaded config from %s", self._config_path)
        return self._config

    def save_config(self, config: AppConfig) -> None:
        """Save configuration to YAML file.

        Creates the parent directory if it does not exist.

        Args:
            config: The application configuration to persist.
        """
        self._config_path.parent.mkdir(parents=True, exist_ok=True)

        data = config.model_dump(mode="json", exclude_none=True)
        yaml_text = yaml.dump(data, default_flow_style=False, sort_keys=False)

        self._config_path.write_text(yaml_text, encoding="utf-8")
        self._config = config
        logger.info("Saved config to %s", self._config_path)

    def get_config(self) -> AppConfig:
        """Get the current in-memory config, loading from disk if needed.

        Returns:
            The current application configuration.
        """
        if self._config is None:
            return self.load_config()
        return self._config

    # ------------------------------------------------------------------
    # Destination profile operations
    # ------------------------------------------------------------------

    def get_destinations(self) -> list[DestinationProfile]:
        """Get all destination profiles.

        Returns:
            List of destination profiles from config.
        """
        return list(self.get_config().destinations)

    def get_destination(self, name: str) -> DestinationProfile | None:
        """Find a destination profile by name.

        Args:
            name: The destination name to find.

        Returns:
            The matching destination profile, or None if not found.
        """
        for dest in self.get_config().destinations:
            if dest.name == name:
                return dest
        return None

    def save_destination(self, dest: DestinationProfile) -> None:
        """Add or update a destination profile.

        If a destination with the same name exists, it is replaced
        **in place** (preserving list order).  Otherwise, the
        destination is appended to the end of the list.

        Args:
            dest: The destination profile to save.
        """
        config = self.get_config()
        replaced = False
        destinations: list[DestinationProfile] = []
        for d in config.destinations:
            if d.name == dest.name:
                destinations.append(dest)
                replaced = True
            else:
                destinations.append(d)
        if not replaced:
            destinations.append(dest)
        updated = config.model_copy(update={"destinations": destinations})
        self.save_config(updated)

    def remove_destination(self, name: str) -> bool:
        """Remove a destination profile by name.

        Args:
            name: The destination name to remove.

        Returns:
            True if the destination was found and removed, False otherwise.
        """
        config = self.get_config()
        original_count = len(config.destinations)
        destinations = [d for d in config.destinations if d.name != name]

        if len(destinations) == original_count:
            return False

        updated = config.model_copy(update={"destinations": destinations})
        self.save_config(updated)
        return True

    # ------------------------------------------------------------------
    # Input source configuration
    # ------------------------------------------------------------------

    def get_input_config(self) -> InputProfile | None:
        """Get the current input source configuration.

        Returns:
            The input profile, or None if not configured.
        """
        return self.get_config().input

    def save_input_config(self, input_config: InputProfile) -> None:
        """Save the input source configuration.

        Args:
            input_config: The input profile to persist.
        """
        config = self.get_config()
        updated = config.model_copy(update={"input": input_config})
        self.save_config(updated)

    # ------------------------------------------------------------------
    # Application settings
    # ------------------------------------------------------------------

    def get_settings(self) -> AppSettings:
        """Get application settings.

        Returns:
            The current application settings.
        """
        return self.get_config().settings

    def save_settings(self, settings: AppSettings) -> None:
        """Save application settings.

        Args:
            settings: The settings to persist.
        """
        config = self.get_config()
        updated = config.model_copy(update={"settings": settings})
        self.save_config(updated)

    # ------------------------------------------------------------------
    # Device profile
    # ------------------------------------------------------------------

    def get_device_profile(self) -> DeviceProfile | None:
        """Get the persisted device connection settings.

        Returns:
            The device profile, or None if never saved.
        """
        return self.get_config().device

    def save_device_profile(self, profile: DeviceProfile) -> None:
        """Persist device connection settings (port, baud, vendor).

        Args:
            profile: The device profile to save.
        """
        config = self.get_config()
        updated = config.model_copy(update={"device": profile})
        self.save_config(updated)

    # ------------------------------------------------------------------
    # Base station positions
    # ------------------------------------------------------------------

    def get_base_positions(self) -> list[BaseStationPosition]:
        """Return all saved base station position profiles.

        Returns:
            List of named position profiles.
        """
        return list(self.get_config().base_positions)

    def get_base_position(self, name: str) -> BaseStationPosition | None:
        """Lookup a base position by name.

        Args:
            name: Profile name (case-sensitive).

        Returns:
            The matching position, or None if not found.
        """
        for pos in self.get_config().base_positions:
            if pos.name == name:
                return pos
        return None

    def save_base_position(self, position: BaseStationPosition) -> None:
        """Save or update a named base station position.

        If a position with the same name already exists it is replaced.

        Args:
            position: The position profile to persist.
        """
        config = self.get_config()
        positions = [p for p in config.base_positions if p.name != position.name]
        positions.append(position)
        updated = config.model_copy(update={"base_positions": positions})
        self.save_config(updated)

    def delete_base_position(self, name: str) -> bool:
        """Delete a saved base station position by name.

        Args:
            name: Profile name to delete.

        Returns:
            True if the position was found and deleted, False otherwise.
        """
        config = self.get_config()
        positions = [p for p in config.base_positions if p.name != name]
        if len(positions) == len(config.base_positions):
            return False
        updated = config.model_copy(update={"base_positions": positions})
        self.save_config(updated)
        return True
