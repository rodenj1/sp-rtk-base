"""Profile persistence — the only filesystem toucher for GPS profiles.

Merges built-in profiles (read-only, shipped in the package) with
custom profiles (one YAML file each, under a resolved profiles
directory) behind a single read/write API. Path resolution follows
the same precedence as :mod:`sp_rtk_base.services.config_service`:
explicit path -> environment override -> default.

A corrupt or schema-invalid custom file is skipped with a warning on
load — never fatal — so one bad file can't take the picker down.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from sp_rtk_base.models.profile_models import Profile
from sp_rtk_base.profiles import BUILTIN_PROFILES

logger = logging.getLogger(__name__)

DEFAULT_PROFILES_DIR = Path.home() / ".config" / "sp-rtk-base" / "profiles"
ENV_PROFILES_DIR = "SP_RTK_BASE_PROFILES_DIR"

#: Custom profile names must be filesystem- and URL-safe — this is the
#: same slug rule the repo already applies to other named YAML records
#: (see ``BaseStationPosition.name`` in config_service). Enforcing it
#: here (rather than in the schema) keeps the schema hardware-agnostic
#: about storage and lets the store reject path-unsafe names (e.g. a
#: name containing ``/`` or ``..``) before they ever reach the filesystem.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ProfileStoreError(Exception):
    """Base class for ProfileStore errors."""


class ProfileNotFoundError(ProfileStoreError):
    """Raised when a named profile (built-in or custom) does not exist."""


class ProfileConflictError(ProfileStoreError):
    """Raised when a name collides with an existing built-in or custom."""


class ProfileImmutableError(ProfileStoreError):
    """Raised when an operation would modify, rename, or delete a built-in."""


class ProfileBusinessRuleError(ProfileStoreError):
    """Raised for a business-rule rejection that isn't a schema violation."""


def _get_profiles_dir() -> Path:
    """Resolve the custom-profiles directory.

    Checks the ``SP_RTK_BASE_PROFILES_DIR`` environment variable first,
    then falls back to ``~/.config/sp-rtk-base/profiles``.
    """
    env_path = os.environ.get(ENV_PROFILES_DIR)
    if env_path:
        return Path(env_path)
    return DEFAULT_PROFILES_DIR


class ProfileStore:
    """Merges built-in and custom GPS receiver profiles.

    Built-ins ship read-only in the package (:data:`BUILTIN_PROFILES`)
    and are never modified, overwritten, or deleted. Customs are one
    YAML file per profile under :attr:`profiles_dir`.

    Args:
        profiles_dir: Optional explicit path to the custom-profiles
            directory. If not provided, uses ``SP_RTK_BASE_PROFILES_DIR``
            env var or the default ``~/.config/sp-rtk-base/profiles``.
    """

    def __init__(self, profiles_dir: Path | None = None) -> None:
        self._profiles_dir = profiles_dir or _get_profiles_dir()

    @property
    def profiles_dir(self) -> Path:
        """The resolved custom-profiles directory."""
        return self._profiles_dir

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[Profile]:
        """List every profile, built-ins before customs, alphabetical within each.

        Returns:
            Built-in profiles sorted by name, followed by custom
            profiles sorted by name.
        """
        customs = self._load_customs()
        builtins_sorted = sorted(BUILTIN_PROFILES.values(), key=lambda p: p.name)
        customs_sorted = sorted(customs.values(), key=lambda p: p.name)
        return builtins_sorted + customs_sorted

    def get_profile(self, name: str) -> Profile | None:
        """Look up a profile by name across built-ins and customs.

        Args:
            name: Profile name.

        Returns:
            The matching profile, or None if no built-in or custom
            profile has that name.
        """
        builtin = BUILTIN_PROFILES.get(name)
        if builtin is not None:
            return builtin
        return self._load_customs().get(name)

    def is_builtin(self, name: str) -> bool:
        """Return True if *name* identifies a shipped built-in profile."""
        return name in BUILTIN_PROFILES

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_profile(self, profile: Profile) -> Profile:
        """Persist a new custom profile.

        Args:
            profile: The profile to create.

        Returns:
            The created profile.

        Raises:
            ProfileBusinessRuleError: The name isn't filesystem-safe.
            ProfileConflictError: The name collides with a built-in or
                an existing custom profile.
        """
        self._validate_name_is_safe(profile.name)
        if profile.name in BUILTIN_PROFILES:
            raise ProfileConflictError(
                f"'{profile.name}' collides with a built-in profile"
            )
        if profile.name in self._load_customs():
            raise ProfileConflictError(
                f"A custom profile named '{profile.name}' already exists"
            )
        self._write_custom(profile)
        logger.info("Created custom profile: %s", profile.name)
        return profile

    def rename_profile(self, name: str, new_name: str) -> Profile:
        """Rename an existing custom profile.

        Args:
            name: Current name of the custom profile.
            new_name: New name to give it.

        Returns:
            The renamed profile.

        Raises:
            ProfileImmutableError: *name* identifies a built-in.
            ProfileNotFoundError: No custom profile named *name* exists.
            ProfileBusinessRuleError: *new_name* isn't filesystem-safe.
            ProfileConflictError: *new_name* collides with a built-in or
                another existing custom profile.
        """
        if name in BUILTIN_PROFILES:
            raise ProfileImmutableError(f"'{name}' is a built-in and cannot be renamed")
        customs = self._load_customs()
        existing = customs.get(name)
        if existing is None:
            raise ProfileNotFoundError(f"No profile named '{name}'")

        if new_name == name:
            return existing

        self._validate_name_is_safe(new_name)
        if new_name in BUILTIN_PROFILES:
            raise ProfileConflictError(f"'{new_name}' collides with a built-in profile")
        if new_name in customs:
            raise ProfileConflictError(
                f"A custom profile named '{new_name}' already exists"
            )

        renamed = existing.model_copy(update={"name": new_name})
        self._write_custom(renamed)
        self._custom_path(name).unlink()
        logger.info("Renamed custom profile: %s -> %s", name, new_name)
        return renamed

    def delete_profile(self, name: str) -> None:
        """Delete a custom profile.

        Args:
            name: Name of the custom profile to delete.

        Raises:
            ProfileImmutableError: *name* identifies a built-in.
            ProfileNotFoundError: No custom profile named *name* exists.
        """
        if name in BUILTIN_PROFILES:
            raise ProfileImmutableError(f"'{name}' is a built-in and cannot be deleted")
        if name not in self._load_customs():
            raise ProfileNotFoundError(f"No profile named '{name}'")
        self._custom_path(name).unlink()
        logger.info("Deleted custom profile: %s", name)

    def export_profile(self, name: str) -> Profile:
        """Fetch a profile for export.

        Args:
            name: Profile name (built-in or custom).

        Returns:
            The profile, in the same shape :meth:`import_profile` accepts.

        Raises:
            ProfileNotFoundError: No profile named *name* exists.
        """
        profile = self.get_profile(name)
        if profile is None:
            raise ProfileNotFoundError(f"No profile named '{name}'")
        return profile

    def import_profile(self, data: dict[str, Any]) -> Profile:
        """Validate and persist an exported profile document.

        Args:
            data: A profile document, as produced by :meth:`export_profile`.

        Returns:
            The imported (created) profile.

        Raises:
            pydantic.ValidationError: *data* fails schema validation,
                including an unknown ``version``.
            ProfileBusinessRuleError: The name isn't filesystem-safe.
            ProfileConflictError: The name collides with a built-in or
                an existing custom profile.
        """
        profile = Profile.model_validate(data)
        return self.create_profile(profile)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _validate_name_is_safe(self, name: str) -> None:
        if not _SAFE_NAME_RE.match(name):
            raise ProfileBusinessRuleError(
                f"profile name {name!r} must match ^[A-Za-z0-9_-]+$"
            )

    def _custom_path(self, name: str) -> Path:
        return self._profiles_dir / f"{name}.yaml"

    def _load_customs(self) -> dict[str, Profile]:
        profiles: dict[str, Profile] = {}
        if not self._profiles_dir.exists():
            return profiles
        for path in sorted(self._profiles_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                profile = Profile.model_validate(data)
            except (yaml.YAMLError, ValidationError) as exc:
                logger.warning("Skipping malformed custom profile %s: %s", path, exc)
                continue
            profiles[profile.name] = profile
        return profiles

    def _write_custom(self, profile: Profile) -> None:
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        data = profile.model_dump(mode="json", exclude_none=True)
        yaml_text = yaml.dump(data, default_flow_style=False, sort_keys=False)
        self._custom_path(profile.name).write_text(yaml_text, encoding="utf-8")
