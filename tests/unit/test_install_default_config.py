"""Regression test: the default config written by ``deploy/install.sh``
must validate against the current ``AppConfig`` pydantic model.

This catches schema drift between the installer's YAML heredoc and the
model — e.g. the 2026-05-26 production crash where ``install.sh`` was
writing ``input.source_type`` / ``input.tcp_host`` / ``input.tcp_port``
while ``InputProfile`` only accepts ``source`` + ``config``.
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
def default_config_yaml() -> str:
    """Extract the heredoc-embedded default ``config.yaml`` from
    ``deploy/install.sh``.
    """
    assert INSTALL_SCRIPT.is_file(), f"installer not found: {INSTALL_SCRIPT}"
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    match = _HEREDOC_RE.search(text)
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
