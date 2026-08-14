"""Strict config loader for headless network provisioning (issue #9).

Loads :class:`NetProvisionConfig` from its own YAML file, separate from
the app's ``config.yaml`` (:mod:`sp_rtk_base.services.config_service`):
the provisioning service runs independently of ``sp-rtk-base.service``
(issue #6, story 17) and must not read a file the web UI rewrites.

This is the opposite failure mode from ``ConfigService``. That service
falls back to defaults on a missing/empty/malformed file so the app
always has something to run with. This loader cannot do that:
``ap_password`` is deliberately required with no default, so there is
no valid default config — every failure here raises loudly instead.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from sp_rtk_base.models.net_provision_models import NetProvisionConfig

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "sp-rtk-base"
DEFAULT_CONFIG_FILENAME = "net_provision.yaml"
ENV_CONFIG_PATH = "SP_RTK_BASE_NET_CONFIG"


class NetProvisionConfigError(RuntimeError):
    """The net-provisioning config file is missing, unreadable, or invalid."""


def get_net_provision_config_path() -> Path:
    """Resolve the config path: ``SP_RTK_BASE_NET_CONFIG`` env var, else default.

    Returns:
        Resolved path to the net-provisioning config file.
    """
    env_path = os.environ.get(ENV_CONFIG_PATH)
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILENAME


def load_net_provision_config(path: Path | None = None) -> NetProvisionConfig:
    """Load and validate the provisioning config, or fail loudly.

    Args:
        path: Overrides the resolved path — mainly for tests. Production
            callers should omit this and rely on ``SP_RTK_BASE_NET_CONFIG``.

    Returns:
        The validated config.

    Raises:
        NetProvisionConfigError: The file is missing, unreadable, empty,
            not a YAML mapping, or fails :class:`NetProvisionConfig`
            validation (including a missing ``ap_password``).
    """
    resolved = path if path is not None else get_net_provision_config_path()

    if not resolved.exists():
        raise NetProvisionConfigError(
            f"Net-provisioning config not found at {resolved}. Create it "
            f"(ap_password is required and has no default), or point "
            f"{ENV_CONFIG_PATH} at an existing file."
        )
    try:
        raw_text = resolved.read_text()
    except OSError as exc:
        raise NetProvisionConfigError(
            f"Could not read net-provisioning config at {resolved}: {exc}"
        ) from exc

    try:
        data: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise NetProvisionConfigError(
            f"Net-provisioning config at {resolved} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(data, dict):
        got = "an empty file" if data is None else f"a {type(data).__name__}"
        raise NetProvisionConfigError(
            f"Net-provisioning config at {resolved} must be a YAML mapping "
            f"of settings, got {got}."
        )

    try:
        return NetProvisionConfig.model_validate(data)
    except ValidationError as exc:
        raise NetProvisionConfigError(
            f"Net-provisioning config at {resolved} failed validation: {exc}"
        ) from exc
