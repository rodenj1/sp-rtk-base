"""Regression tests for the network-provisioning wiring in ``deploy/install.sh``
and ``deploy/uninstall.sh`` (issue #11).

Mirrors the static-analysis approach in ``test_install_default_config.py``:
these scripts need root, apt, systemd, and nmcli to actually run, so the
unit suite instead pins the *text* of the installer against regressions —
schema drift in the written config, and the specific behaviors the
acceptance criteria call out (only-if-absent config, a required fixed AP
password with no source-baked default, an idempotent AP connection
profile, and symmetric cleanup in uninstall.sh).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from sp_rtk_base.models.net_provision_models import NetProvisionConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "deploy" / "install.sh"
UNINSTALL_SCRIPT = REPO_ROOT / "deploy" / "uninstall.sh"

# Matches the unquoted heredoc `cat >"$net_provision_cfg" <<YAML … YAML` —
# unlike config.yaml's heredoc this one is unquoted so $AP_SSID/$AP_PASSWORD
# interpolate, which is also why it can't be fed to yaml.safe_load as-is.
_HEREDOC_RE = re.compile(
    r"cat\s*>\s*\"?\$\{?net_provision_cfg\}?\"?\s*<<YAML\n(?P<body>.*?)\nYAML\b",
    re.DOTALL,
)


@pytest.fixture(scope="module")
def install_script_text() -> str:
    assert INSTALL_SCRIPT.is_file(), f"installer not found: {INSTALL_SCRIPT}"
    return INSTALL_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def uninstall_script_text() -> str:
    assert UNINSTALL_SCRIPT.is_file(), f"uninstaller not found: {UNINSTALL_SCRIPT}"
    return UNINSTALL_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def net_provision_yaml_template(install_script_text: str) -> str:
    """Extract the heredoc-embedded ``net_provision.yaml`` template."""
    match = _HEREDOC_RE.search(install_script_text)
    assert match, (
        "Could not locate the net_provision.yaml heredoc in deploy/install.sh. "
        "If you reformatted the heredoc, update _HEREDOC_RE in this test."
    )
    return match.group("body")


class TestNetProvisionConfigTemplate:
    """Schema-drift regression suite for the installer's net_provision.yaml."""

    def test_template_validates_against_net_provision_config(
        self, net_provision_yaml_template: str
    ) -> None:
        """The heredoc, with its shell vars substituted, validates against
        ``NetProvisionConfig``.

        ``$AP_SSID``/``$AP_PASSWORD`` are interpolated by bash before the
        file is written, so this test substitutes stand-in values first —
        it's checking the YAML *shape* the installer produces, not a
        literal value.
        """
        rendered = net_provision_yaml_template.replace(
            "${ap_ssid_yaml}", "test-ssid"
        ).replace("${ap_password_yaml}", "test-password-1234")
        data = yaml.safe_load(rendered)
        assert isinstance(data, dict), (
            f"net_provision.yaml template did not parse to a mapping; "
            f"got {type(data).__name__}"
        )
        cfg = NetProvisionConfig.model_validate(data)
        assert cfg.ap_ssid == "test-ssid"
        assert cfg.ap_password == "test-password-1234"

    def test_template_references_escaped_ap_ssid_and_ap_password_vars(
        self, net_provision_yaml_template: str
    ) -> None:
        """The template must interpolate the *escaped* SSID/password
        variables, not literal/hardcoded values or the raw $AP_SSID/
        $AP_PASSWORD — otherwise every deployed unit could end up with
        the same source-committed password (defeating issue #11's
        AP_PASSWORD-from-outside-the-repo requirement), or a password
        containing `"`/`\\` could break out of the YAML string.
        """
        assert "${ap_ssid_yaml}" in net_provision_yaml_template
        assert "${ap_password_yaml}" in net_provision_yaml_template


class TestApCredentialsYamlEscaping:
    """A WPA2 passphrase may contain `"` or `\\`, either of which would
    corrupt the YAML double-quoted scalar (or inject a new key) if
    interpolated into the heredoc unescaped.
    """

    def test_escape_helper_is_defined_and_used(self, install_script_text: str) -> None:
        assert "yaml_dq_escape()" in install_script_text
        assert 'ap_ssid_yaml="$(yaml_dq_escape "$AP_SSID")"' in install_script_text
        assert (
            'ap_password_yaml="$(yaml_dq_escape "$AP_PASSWORD")"' in install_script_text
        )

    def test_escape_helper_escapes_backslash_and_double_quote(
        self, install_script_text: str
    ) -> None:
        """Extract the *actual* ``yaml_dq_escape`` function out of
        install.sh and run it for real, rather than re-implementing the
        escaping logic here (which would drift from the real function
        silently the moment either one changes).
        """
        import subprocess

        match = re.search(r"yaml_dq_escape\(\) \{.*?\}", install_script_text)
        assert match, "yaml_dq_escape() function not found in install.sh"
        script = f'{match.group(0)}\nyaml_dq_escape "$1"'
        result = subprocess.run(
            ["bash", "-c", script, "_", 'we"ird\\pass'],
            capture_output=True,
            text=True,
            check=True,
        )
        rendered = f'ap_password: "{result.stdout}"'
        data = yaml.safe_load(rendered)
        assert data["ap_password"] == 'we"ird\\pass'


class TestInstallerRequiresApPassword:
    """AP_PASSWORD must be supplied externally; issue #6 story 8 requires
    one fixed SSID/password across the whole fleet, so the installer must
    not silently synthesise a different password per device.
    """

    def test_ap_password_has_no_baked_in_default(
        self, install_script_text: str
    ) -> None:
        assert 'AP_PASSWORD="${AP_PASSWORD:-}"' in install_script_text, (
            "AP_PASSWORD must default to empty, not a literal password — "
            "a non-empty default would ship a fleet-wide hotspot password "
            "baked into source, which NetProvisionConfig.ap_password's "
            "docstring explicitly forbids."
        )

    def test_ap_ssid_defaults_to_model_default(self, install_script_text: str) -> None:
        assert 'AP_SSID="${AP_SSID:-sp-rtk-base-setup}"' in install_script_text, (
            "AP_SSID's shell default must match NetProvisionConfig.DEFAULT_AP_SSID"
        )

    def test_fails_loudly_when_ap_password_unset_and_config_absent(
        self, install_script_text: str
    ) -> None:
        """Only-if-absent config-writing must be gated on AP_PASSWORD, and
        must ``die`` (not warn-and-continue) when it's missing — a silent
        skip would leave sp-rtk-base-net-provision.service failing with no
        actionable message pointing at the real cause.
        """
        assert re.search(
            r'if\s*\[\[\s*!\s*-e\s*"\$net_provision_cfg"\s*\]\];\s*then'
            r'\s*\n\s*if\s*\[\[\s*-z\s*"\$AP_PASSWORD"\s*\]\];\s*then\s*\n\s*die',
            install_script_text,
        ), (
            "deploy/install.sh must die() with an actionable message when "
            "AP_PASSWORD is unset and net_provision.yaml doesn't exist yet."
        )


class TestInstallerConfigIdempotency:
    """Re-running install.sh must never clobber a site's provisioned
    net_provision.yaml (acceptance criterion, issue #11).
    """

    def test_config_written_only_if_absent(self, install_script_text: str) -> None:
        assert 'if [[ ! -e "$net_provision_cfg" ]]; then' in install_script_text

    def test_ownership_healed_unconditionally(self, install_script_text: str) -> None:
        """chown/chmod on net_provision.yaml must run outside the
        only-if-absent branch (same pattern as config.yaml above it),
        so a pre-existing root-owned file gets healed on every re-run
        instead of only on first write.
        """
        idx = install_script_text.index('if [[ ! -e "$net_provision_cfg" ]]; then')
        tail = install_script_text[idx:]
        # The healing chown/chmod must appear after the closing `fi` of
        # the only-if-absent if/else, not inside either branch.
        fi_idx = tail.index("\nfi\n")
        after_if = tail[fi_idx:]
        assert (
            'chown "${SERVICE_USER}:${SERVICE_USER}" "$net_provision_cfg"' in after_if
        )
        assert 'chmod 0640 "$net_provision_cfg"' in after_if


class TestNetworkManagerEnsured:
    """Acceptance criterion: NetworkManager ensured/installed."""

    def test_installs_network_manager_if_missing(
        self, install_script_text: str
    ) -> None:
        assert "command -v nmcli" in install_script_text
        assert "network-manager" in install_script_text

    def test_enables_network_manager_service(self, install_script_text: str) -> None:
        assert "systemctl enable --now NetworkManager.service" in install_script_text

    def test_warns_if_wlan0_is_unmanaged(self, install_script_text: str) -> None:
        """Acceptance criterion says NetworkManager must be "managing
        wlan0" — install.sh doesn't force a fix (that would mean
        forcibly stopping a possibly-live dhcpcd mid curl|bash, which is
        exactly the kind of surprise a headless install shouldn't
        spring), but it must at least detect and surface the failure
        mode instead of silently leaving the AP unable to ever come up.
        """
        assert (
            'device status 2>/dev/null | awk -F: \'$1=="wlan0"' in install_script_text
        )
        assert 'if [[ "$wlan0_state" == "unmanaged" ]]; then' in install_script_text


class TestApConnectionProfile:
    """Acceptance criterion: AP connection profile installed, idempotently.

    NmcliAdapter (issue #8) only ever activates a connection named
    ``ap_ssid`` via `nmcli connection up/down id <ap_ssid>` — it never
    creates one. install.sh owns creating that profile, and must do so
    from whatever is actually in net_provision.yaml (so a re-run after a
    hand-edited config stays in sync), not directly from $AP_SSID/
    $AP_PASSWORD.
    """

    def test_checks_for_existing_profile_before_creating(
        self, install_script_text: str
    ) -> None:
        assert (
            'nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$provisioned_ap_ssid"'
            in install_script_text
        )

    def test_creates_profile_from_config_not_raw_env_vars(
        self, install_script_text: str
    ) -> None:
        add_idx = install_script_text.index("nmcli connection add")
        block = install_script_text[add_idx : add_idx + 600]
        assert 'con-name "$provisioned_ap_ssid"' in block
        assert 'ssid "$provisioned_ap_ssid"' in block
        assert 'wifi-sec.psk "$provisioned_ap_password"' in block

    def test_uses_property_separator_for_raw_settings(
        self, install_script_text: str
    ) -> None:
        """nmcli's documented syntax requires a literal `--` before raw
        <setting>.<property> overrides (ipv4.method, wifi-sec.*, etc.) —
        see `nmcli connection add help`.
        """
        add_idx = install_script_text.index("nmcli connection add")
        block = install_script_text[add_idx : add_idx + 600]
        assert re.search(r"--\s*\\\n\s*802-11-wireless\.band", block)
        assert "ipv4.method shared" in block
        assert "wifi-sec.key-mgmt wpa-psk" in block


class TestUninstallerRemovesApProfile:
    """Acceptance criterion: uninstall.sh removes the provisioning unit
    (already covered elsewhere), the AP connection profile, and config.
    """

    def test_captures_ap_ssid_before_removing_config(
        self, uninstall_script_text: str
    ) -> None:
        capture_idx = uninstall_script_text.index("AP_SSID=")
        config_rm_idx = uninstall_script_text.index('rm -rf "$CONFIG_DIR"')
        assert capture_idx < config_rm_idx, (
            "uninstall.sh must read ap_ssid out of net_provision.yaml before "
            "any step that could remove CONFIG_DIR or the venv used to "
            "parse it."
        )

    def test_parses_ap_ssid_with_the_venv_pyyaml_not_a_shell_regex(
        self, uninstall_script_text: str
    ) -> None:
        """A plain sed/regex extraction can't correctly round-trip a
        quoted SSID containing spaces or YAML escapes the way install.sh
        wrote it — use the still-intact venv's PyYAML instead (the venv
        isn't removed until later in the script).
        """
        capture_idx = uninstall_script_text.index("AP_SSID=")
        venv_idx = uninstall_script_text.index('venv_python="${INSTALL_PREFIX}')
        rm_prefix_idx = uninstall_script_text.index('rm -rf "$INSTALL_PREFIX"')
        assert capture_idx < venv_idx < rm_prefix_idx, (
            "uninstall.sh must resolve venv_python and parse net_provision.yaml "
            "before INSTALL_PREFIX (and the venv inside it) is removed."
        )
        assert "import yaml" in uninstall_script_text

    def test_deletes_nm_connection_profile(self, uninstall_script_text: str) -> None:
        assert 'nmcli connection delete id "$AP_SSID"' in uninstall_script_text

    def test_ap_profile_removal_is_unconditional_not_gated_by_ask(
        self, uninstall_script_text: str
    ) -> None:
        """The AP profile is infrastructure installed by install.sh, not
        site data — it should be removed unconditionally like the
        systemd units and polkit rule, not gated behind the interactive
        --keep-data prompt used for config/state directories.
        """
        delete_idx = uninstall_script_text.index(
            'nmcli connection delete id "$AP_SSID"'
        )
        preceding = uninstall_script_text[:delete_idx]
        # No `ask` call between the start of the script and this point
        # should gate this specific line — check the immediate guard
        # instead uses a plain command-existence/grep condition.
        block_start = preceding.rindex("\nif ")
        guard = uninstall_script_text[block_start:delete_idx]
        assert "ask(" not in guard
