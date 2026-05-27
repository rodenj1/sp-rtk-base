"""Regression tests for ``deploy/install.sh``.

Two classes of bug have already bitten production at install time:

1. **Schema drift** between the installer's YAML heredoc and ``AppConfig``
   (e.g. the 2026-05-26 crash where ``install.sh`` wrote
   ``input.source_type`` / ``input.tcp_host`` / ``input.tcp_port`` while
   ``InputProfile`` only accepts ``source`` + ``config``).  Covered by
   :class:`TestInstallerDefaultConfig`.

2. **Filesystem permissions** that leave the on-disk config file
   unwritable by the service user (the 2026-05-27 EACCES
   ``[Errno 13] Permission denied: '/etc/sp-rtk-base/config.yaml'`` when
   saving Bluetooth settings from the web UI — the file ended up
   ``root:sp-rtk-base 0640``, group-readable only).  Covered by
   :class:`TestInstallerConfigPermissions`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from sp_rtk_base.models.config_models import AppConfig

# Repo root resolved relative to this test file:
#   tests/unit/test_install_default_config.py  →  ../../  →  repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "deploy" / "install.sh"

# Matches a bash heredoc of the form:  cat >"$default_cfg" <<'YAML' … YAML
_HEREDOC_RE = re.compile(
    r"cat\s*>\s*\"?\$\{?default_cfg\}?\"?\s*<<'YAML'\n(?P<body>.*?)\nYAML\b",
    re.DOTALL,
)


@pytest.fixture(scope="module")
def install_script_text() -> str:
    """Raw text of ``deploy/install.sh`` (used by both test classes)."""
    assert INSTALL_SCRIPT.is_file(), f"installer not found: {INSTALL_SCRIPT}"
    return INSTALL_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def default_config_yaml(install_script_text: str) -> str:
    """Extract the heredoc-embedded default ``config.yaml`` from
    ``deploy/install.sh``.
    """
    match = _HEREDOC_RE.search(install_script_text)
    assert match, (
        "Could not locate the default-config heredoc in deploy/install.sh.  "
        "If you reformatted the heredoc, update _HEREDOC_RE in this test."
    )
    return match.group("body")


class TestInstallerDefaultConfig:
    """Schema-drift regression suite for the installer's default config."""

    def test_heredoc_is_valid_yaml(self, default_config_yaml: str) -> None:
        """The heredoc body parses as YAML (sanity check)."""
        parsed = yaml.safe_load(default_config_yaml)
        assert isinstance(parsed, dict), (
            f"Default config did not parse to a mapping; got {type(parsed).__name__}"
        )

    def test_heredoc_validates_against_appconfig(
        self, default_config_yaml: str
    ) -> None:
        """The installer's default config validates against ``AppConfig``.

        If this test fails, ``deploy/install.sh`` will write a config that
        causes ``sp-rtk-base`` to crash on first start.  Either:
          1. Fix the heredoc in ``deploy/install.sh`` to match the current
             ``AppConfig`` / ``InputProfile`` / ``AppSettings`` schema, **or**
          2. Update the model to accept what the heredoc writes (and update
             ``docs/deployment-pi.md`` to match).
        """
        data = yaml.safe_load(default_config_yaml)
        cfg = AppConfig.model_validate(data)

        # Sensible expectations from the documented default — keep this in
        # sync with ``docs/deployment-pi.md`` so docs/code/installer agree.
        assert cfg.settings.metrics_enabled is True
        assert cfg.destinations == []
        assert cfg.base_positions == []
        assert cfg.input is None, (
            "Default config should NOT pin an input source; the operator "
            "selects one from the Input page on first launch.  If you have a "
            "good reason to ship a default input, make sure the heredoc "
            "produces a valid InputProfile (fields: source, config)."
        )


class TestInstallerConfigPermissions:
    """Regression: the installer must lay the config file down writable
    by the service user.

    Background: the v0.2.x installer did ``chown root:${SERVICE_USER}``
    + ``chmod 0640`` on ``/etc/sp-rtk-base/config.yaml``, which gives the
    service user **read-only** access.  The systemd unit runs the app as
    ``User=sp-rtk-base``, so every web-UI save (Input, Bluetooth, etc.)
    crashed with ``[Errno 13] Permission denied: '/etc/sp-rtk-base/config.yaml'``.

    These tests pin the fix in :func:`deploy/install.sh` (Step 7 +
    Step 3) so a future "tighten permissions" patch can't silently
    re-introduce the bug.
    """

    # The chown lines after the heredoc must own the file to the service
    # user, not to root.  We allow either bare ``$SERVICE_USER`` or the
    # braced form ``${SERVICE_USER}`` on both sides of the colon.
    _CONFIG_CHOWN_RE = re.compile(
        r'chown\s+"\$\{?SERVICE_USER\}?:\$\{?SERVICE_USER\}?"\s+"\$default_cfg"'
    )
    _CONFIG_CHMOD_RE = re.compile(r'chmod\s+(0?6[46]0)\s+"\$default_cfg"')

    # The ``install -d`` line for CONFIG_DIR (and the heal-chown that
    # follows) must own the directory to the service user too, so atomic-
    # rename saves work when we move to that pattern.
    _CONFIG_DIR_INSTALL_RE = re.compile(
        r'install\s+-d\s+-m\s+0750\s+-o\s+"\$SERVICE_USER"\s+-g\s+"\$SERVICE_USER"\s+"\$CONFIG_DIR"'
    )
    _CONFIG_DIR_CHOWN_RE = re.compile(
        r'chown\s+"\$SERVICE_USER:\$SERVICE_USER"\s+"\$CONFIG_DIR"'
    )

    def test_config_file_chown_targets_service_user(
        self, install_script_text: str
    ) -> None:
        """The installer must chown config.yaml to the service user.

        The original bug was ``chown "root:${SERVICE_USER}"`` which made
        the file read-only for the service.  Must be
        ``chown "${SERVICE_USER}:${SERVICE_USER}"`` so the running
        service can write back changes from the web UI.
        """
        assert self._CONFIG_CHOWN_RE.search(install_script_text), (
            "deploy/install.sh must chown ${default_cfg} to "
            "${SERVICE_USER}:${SERVICE_USER} (NOT root:${SERVICE_USER}), "
            "or the running service can't save config changes.  See the "
            "2026-05-27 EACCES regression for context."
        )
        # And the broken form must not still be lingering anywhere.
        assert (
            'chown "root:${SERVICE_USER}" "$default_cfg"' not in install_script_text
        ), (
            "Found legacy 'chown root:${SERVICE_USER}' on $default_cfg.  "
            "This makes the file read-only for the service user; remove "
            "it and use 'chown ${SERVICE_USER}:${SERVICE_USER}' instead."
        )

    def test_config_file_chmod_allows_owner_write(
        self, install_script_text: str
    ) -> None:
        """The installer must chmod config.yaml to a writable mode for owner.

        Acceptable: 0640 (owner rw, group r) or 0660 (owner rw, group rw).
        Combined with the chown-to-service-user above, both modes leave
        the file writable by the running service.
        """
        match = self._CONFIG_CHMOD_RE.search(install_script_text)
        assert match, (
            "deploy/install.sh must explicitly 'chmod' $default_cfg to "
            "0640 or 0660.  Without an explicit mode the file inherits "
            "umask and behaviour becomes host-specific."
        )

    def test_config_dir_owned_by_service_user(self, install_script_text: str) -> None:
        """The installer must create CONFIG_DIR owned by the service user.

        Required for any future atomic-rename save (``tempfile →
        os.replace``) — the rename target's parent must be writable by
        the renaming process.  Current in-place ``write_text`` doesn't
        strictly need this, but we pin it now so we don't trip a second
        EACCES the moment we harden ``ConfigService.save_config``.
        """
        assert self._CONFIG_DIR_INSTALL_RE.search(install_script_text), (
            "deploy/install.sh must create $CONFIG_DIR owned by "
            "${SERVICE_USER}:${SERVICE_USER}, e.g.\n"
            '    install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" '
            '"$CONFIG_DIR"'
        )

    def test_config_dir_chown_heals_existing_install(
        self, install_script_text: str
    ) -> None:
        """A re-run of the installer must heal a pre-existing
        ``root:sp-rtk-base`` CONFIG_DIR by chown'ing it to the service user.

        Otherwise operators who hit the 2026-05-27 EACCES bug on a v0.2.x
        install would have to manually chown the directory before
        re-running install.sh.
        """
        assert self._CONFIG_DIR_CHOWN_RE.search(install_script_text), (
            "deploy/install.sh must include an unconditional "
            "'chown $SERVICE_USER:$SERVICE_USER $CONFIG_DIR' after the "
            "'install -d' line so a re-run heals pre-existing installs "
            "whose CONFIG_DIR was created root-owned."
        )
