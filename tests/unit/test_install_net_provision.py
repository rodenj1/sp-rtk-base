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
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from sp_rtk_base.models.net_provision_models import NetProvisionConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "deploy" / "install.sh"
UNINSTALL_SCRIPT = REPO_ROOT / "deploy" / "uninstall.sh"
APP_SERVICE_UNIT = REPO_ROOT / "deploy" / "sp-rtk-base.service"
POLKIT_RULE = REPO_ROOT / "deploy" / "polkit" / "10-sp-rtk-base-net-provision.rules"
NET_PROVISION_TEARDOWN_LIB = (
    REPO_ROOT / "deploy" / "shared" / "net-provision-teardown.sh"
)
# The venv running this test suite (via `uv run pytest`) already has
# sp_rtk_base + PyYAML installed — stand in for install.sh's own
# ${VENV_DIR} when executing extracted script fragments for real instead
# of reimplementing their logic in Python (which would drift silently).
VENV_DIR_STANDIN = Path(sys.executable).parent.parent

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
def teardown_lib_text() -> str:
    assert NET_PROVISION_TEARDOWN_LIB.is_file(), (
        f"shared teardown helper not found: {NET_PROVISION_TEARDOWN_LIB}"
    )
    return NET_PROVISION_TEARDOWN_LIB.read_text(encoding="utf-8")


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
    """AP_PASSWORD has no *shell*-level default; issue #6 story 8 requires
    one fixed SSID/password across the whole fleet. Since issue #27
    decision 3, an unset AP_PASSWORD no longer hard-fails the install — it
    falls back to a fixed fleet default at the point it's actually needed
    (Step 8.3, appliance mode only) — see
    :class:`TestApPasswordDefaultsInsteadOfDying` below.
    """

    def test_ap_password_has_no_baked_in_default(
        self, install_script_text: str
    ) -> None:
        assert 'AP_PASSWORD="${AP_PASSWORD:-}"' in install_script_text, (
            "AP_PASSWORD must default to empty at the shell-variable level "
            "— the fleet-default fallback belongs only in the write-time "
            "branch (Step 8.3), not baked in here."
        )

    def test_ap_ssid_defaults_to_model_default(self, install_script_text: str) -> None:
        assert 'AP_SSID="${AP_SSID:-sp-rtk-base-setup}"' in install_script_text, (
            "AP_SSID's shell default must match NetProvisionConfig.DEFAULT_AP_SSID"
        )


class TestApPasswordDefaultsInsteadOfDying:
    """Issue #27 decision 3: appliance mode defaults AP_PASSWORD to a fixed
    fleet password (with a warning) instead of dying, when it's unset and
    net_provision.yaml doesn't exist yet. A fixed, shared password is an
    accepted risk here because the setup AP is transient and printed on a
    physical sticker — the old hard failure just meant every appliance
    install needed an operator to remember to set it.
    """

    def test_defaults_ap_password_when_unset_and_config_absent(
        self, install_script_text: str
    ) -> None:
        assert re.search(
            r'if\s*\[\[\s*!\s*-e\s*"\$net_provision_cfg"\s*\]\];\s*then'
            r'\s*\n\s*if\s*\[\[\s*-z\s*"\$AP_PASSWORD"\s*\]\];\s*then\s*\n'
            r'\s*AP_PASSWORD="sp-rtk-base1234!"',
            install_script_text,
        ), (
            "deploy/install.sh must default AP_PASSWORD to "
            "'sp-rtk-base1234!' (not die()) when unset and "
            "net_provision.yaml doesn't exist yet — issue #27 decision 3."
        )

    def test_warns_when_defaulting_ap_password(self, install_script_text: str) -> None:
        idx = install_script_text.index('AP_PASSWORD="sp-rtk-base1234!"')
        block = install_script_text[idx : idx + 400]
        assert 'warn "AP_PASSWORD is not set' in block

    def test_no_longer_dies_on_unset_ap_password(
        self, install_script_text: str
    ) -> None:
        """Regression guard: the pre-#27 behavior called ``die`` in this
        exact spot — make sure it wasn't just left alongside the new
        default rather than replaced.
        """
        idx = install_script_text.index('if [[ -z "$AP_PASSWORD" ]]; then')
        block = install_script_text[idx : idx + 400]
        assert "die " not in block and "die\n" not in block


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


class TestDnsmasqWildcardDropIn:
    """issue #34: wildcard DNS must be configured into NM's own
    shared-mode dnsmasq, not served by a custom responder — a custom
    UDP/53 bind never sees real client traffic (NM's dnsmasq binds the
    AP's specific gateway IP, which Linux's socket demux always
    prefers over a wildcard 0.0.0.0 bind).
    """

    def test_creates_the_dnsmasq_shared_d_directory(
        self, install_script_text: str
    ) -> None:
        assert 'install -d -m 0755 -o root -g root "$DNSMASQ_SHARED_D"' in (
            install_script_text
        )

    def test_writes_wildcard_answer_from_configured_gateway_ip(
        self, install_script_text: str
    ) -> None:
        assert "address=/#/${provisioned_ap_gateway_ip}" in install_script_text

    def test_gateway_ip_is_read_from_validated_config_not_a_raw_env_var(
        self, install_script_text: str
    ) -> None:
        """Same principle as the AP connection profile (issue #11): built
        from what's actually in net_provision.yaml, so a re-run after a
        hand-edited config stays in sync."""
        assert "print(cfg.ap_gateway_ip)" in install_script_text
        assert (
            'provisioned_ap_gateway_ip="$(sed -n \'3p\' <<<"$net_provision_creds")"'
            in install_script_text
        )


class TestMainAppServiceHasNetProvisionEnv:
    """The Network console page (issues #22-24) reads net-provisioning
    config through the same :func:`load_net_provision_config` the
    provisioning supervisor uses, from inside the *main* app process —
    not just the separate ``sp-rtk-base-net-provision.service``. Without
    ``SP_RTK_BASE_NET_CONFIG`` set here too, the loader falls back to a
    ``~/.config`` path that doesn't exist for the homeless ``sp-rtk-base``
    system user, and every ``/api/network/*`` call 502s in production
    even though the provisioning supervisor itself runs fine.
    """

    def test_sets_net_provision_config_env_var(self) -> None:
        assert APP_SERVICE_UNIT.is_file(), f"unit file not found: {APP_SERVICE_UNIT}"
        text = APP_SERVICE_UNIT.read_text(encoding="utf-8")
        assert (
            "Environment=SP_RTK_BASE_NET_CONFIG=/etc/sp-rtk-base/net_provision.yaml"
            in text
        )


class TestPolkitRuleGrantsWifiShare:
    """Activating the setup AP is a WPA2-PSK shared-mode connection,
    which NetworkManager gates behind the ``wifi.share.protected``
    polkit action specifically — separate from the broader
    ``network-control`` action the rule already grants. Missing it means
    ``nmcli connection up id <ap_ssid>`` fails with "Not authorized to
    share connections via wifi" and the setup AP can never come up,
    silently breaking the one fallback path a stranded device has.
    Caught by deploying to real hardware — no nmcli/polkit mock can
    exercise this, so it's pinned here as a static-text check like the
    rest of this module.
    """

    def test_grants_wifi_share_protected(self) -> None:
        assert POLKIT_RULE.is_file(), f"polkit rule not found: {POLKIT_RULE}"
        text = POLKIT_RULE.read_text(encoding="utf-8")
        assert '"org.freedesktop.NetworkManager.wifi.share.protected"' in text


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

    def test_deletes_nm_connection_profile(self, teardown_lib_text: str) -> None:
        """The delete itself now lives in the shared teardown helper
        (issue #27/#30) — see TestSharedTeardownHelper for the
        uninstall.sh delegation checks.
        """
        assert 'nmcli connection delete id "$ap_ssid"' in teardown_lib_text

    def test_ap_profile_removal_is_unconditional_not_gated_by_ask(
        self, teardown_lib_text: str
    ) -> None:
        """The AP profile is infrastructure installed by install.sh, not
        site data — it should be removed unconditionally like the
        systemd units and polkit rule, not gated behind the interactive
        --keep-data prompt used for config/state directories. The shared
        teardown helper has no `ask()` at all, so this is really just
        confirming the delete isn't wrapped in some other conditional.
        """
        delete_idx = teardown_lib_text.index('nmcli connection delete id "$ap_ssid"')
        preceding = teardown_lib_text[:delete_idx]
        block_start = preceding.rindex("if command -v nmcli")
        guard = teardown_lib_text[block_start:delete_idx]
        assert "ask(" not in guard


class TestSharedTeardownHelper:
    """issue #27/#30 acceptance: no duplicated teardown logic between
    install.sh and uninstall.sh — both delegate to the same sourced
    ``deploy/shared/net-provision-teardown.sh`` function.
    """

    def test_helper_defines_the_teardown_function(self, teardown_lib_text: str) -> None:
        assert "teardown_appliance_network_artifacts()" in teardown_lib_text

    def test_helper_stops_and_disables_net_provision_service(
        self, teardown_lib_text: str
    ) -> None:
        assert "systemctl stop    sp-rtk-base-net-provision.service" in (
            teardown_lib_text
        )
        assert "systemctl disable sp-rtk-base-net-provision.service" in (
            teardown_lib_text
        )

    def test_helper_removes_unit_and_polkit_rule(self, teardown_lib_text: str) -> None:
        assert '-f "$NET_PROVISION_SYSTEMD_UNIT"' in teardown_lib_text
        assert '-f "$POLKIT_RULE_DEST"' in teardown_lib_text

    def test_helper_removes_dnsmasq_wildcard_drop_in(
        self, teardown_lib_text: str
    ) -> None:
        assert '-f "$DNSMASQ_WILDCARD_CONF"' in teardown_lib_text
        assert 'rm -f "$DNSMASQ_WILDCARD_CONF"' in teardown_lib_text

    def test_uninstall_sources_shared_helper(self, uninstall_script_text: str) -> None:
        assert (
            'source "$(dirname "$0")/shared/net-provision-teardown.sh"'
            in uninstall_script_text
        )

    def test_uninstall_calls_shared_function_instead_of_inlining(
        self, uninstall_script_text: str
    ) -> None:
        assert "teardown_appliance_network_artifacts" in uninstall_script_text
        # Regression guard: the pre-#30 uninstall.sh had these steps
        # inlined — make sure they weren't just left duplicated alongside
        # the new delegation.
        assert "nmcli connection delete id" not in uninstall_script_text
        assert 'rm -f "$POLKIT_RULE_DEST"' not in uninstall_script_text

    def test_install_sources_shared_helper_with_curl_fallback(
        self, install_script_text: str
    ) -> None:
        assert "teardown_lib_src=" in install_script_text
        assert 'source "$teardown_lib_src"' in install_script_text
        assert (
            "${REPO_RAW_BASE}/deploy/shared/net-provision-teardown.sh"
            in install_script_text
        )

    def test_install_does_not_duplicate_teardown_steps_either(
        self, install_script_text: str
    ) -> None:
        assert "nmcli connection delete id" not in install_script_text


# ---------------------------------------------------------------------------
# Deployment mode dispatch (issue #27 / #29, T2)
# ---------------------------------------------------------------------------


class TestModeArgParsing:
    """--mode/$MODE parsing must not break the pre-existing positional
    VERSION argument (`./install.sh 0.2.0`) or VERSION= env var usage.
    """

    def test_mode_env_var_has_no_shell_default(self, install_script_text: str) -> None:
        assert 'MODE="${MODE:-}"' in install_script_text, (
            "MODE must default to empty at the shell-variable level — Step "
            "6.5 is what resolves it (from --mode, $MODE, or an existing "
            "config.yaml) and dies if none is available."
        )

    def test_parses_mode_flag_with_space_and_equals_forms(
        self, install_script_text: str
    ) -> None:
        assert "--mode)" in install_script_text
        assert "--mode=*)" in install_script_text

    def test_unrecognized_args_still_populate_version(
        self, install_script_text: str
    ) -> None:
        """Anything that isn't --mode/--mode=... must still be treated as
        the VERSION positional arg — the flag parser must not have dropped
        `./install.sh 0.2.0` support.
        """
        parse_idx = install_script_text.index("while [[ $# -gt 0 ]]; do")
        done_idx = install_script_text.index("\ndone", parse_idx)
        block = install_script_text[parse_idx:done_idx]
        assert 'VERSION="$1"' in block


class _ModeResolutionHarness:
    """Shared fixture + execution helper for Step 6.5 (not a test class
    itself — no ``test_*`` methods here — so subclasses don't re-collect
    and re-run each other's tests).

    Extracts the actual bash block out of install.sh and runs it in
    isolation (stubbing VENV_DIR at this test-suite's own venv, which
    already has sp_rtk_base + PyYAML installed) rather than reimplementing
    the die/preserve/switch decision table here, which would drift from
    the real script silently the moment either one changes.
    """

    @pytest.fixture(scope="class")
    def mode_resolution_block(self, install_script_text: str) -> str:
        start = install_script_text.index("# Step 6.5 — Determine deployment mode")
        end_marker = 'ok "Deployment mode: ${MODE}"'
        end = install_script_text.index(end_marker, start) + len(end_marker)
        return install_script_text[start:end]

    def _resolve(
        self,
        mode_resolution_block: str,
        mode: str,
        config_yaml: str | None,
        net_provision_yaml: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            if config_yaml is not None:
                (Path(td) / "config.yaml").write_text(config_yaml)
            if net_provision_yaml is not None:
                (Path(td) / "net_provision.yaml").write_text(net_provision_yaml)
            # NET_PROVISION_SYSTEMD_UNIT/POLKIT_RULE_DEST point at paths
            # inside the temp dir (never created by this harness), so the
            # real teardown_appliance_network_artifacts() runs for real —
            # every step inside it is already best-effort/idempotent, so
            # this is safe without root, systemd, or nmcli.
            script = f"""
set -euo pipefail
log() {{ echo "LOG: $*"; }}
ok() {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*" >&2; }}
die() {{ echo "DIE: $*" >&2; exit 1; }}
VENV_DIR="{VENV_DIR_STANDIN}"
CONFIG_DIR="{td}"
MODE="{mode}"
NET_PROVISION_SYSTEMD_UNIT="{td}/sp-rtk-base-net-provision.service"
POLKIT_RULE_DEST="{td}/10-sp-rtk-base-net-provision.rules"
DNSMASQ_WILDCARD_CONF="{td}/sp-rtk-base-wildcard.conf"
source "{NET_PROVISION_TEARDOWN_LIB}"
{mode_resolution_block}
echo "RESOLVED_MODE=$MODE"
"""
            return subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True
            )


class TestDeploymentModeResolution(_ModeResolutionHarness):
    """Execution-based tests for Step 6.5's plain die/preserve/override
    MODE resolution (no mode-switch teardown involved) — see
    :class:`TestModeSwitchTeardown` for the teardown-specific cases.
    """

    def test_dies_when_no_mode_and_no_existing_config(
        self, mode_resolution_block: str
    ) -> None:
        result = self._resolve(mode_resolution_block, mode="", config_yaml=None)
        assert result.returncode != 0
        assert "DIE:" in result.stderr

    def test_dies_when_no_mode_and_config_has_no_deployment_section(
        self, mode_resolution_block: str
    ) -> None:
        """A pre-#28 config.yaml (no deployment key at all) must be
        treated the same as no config — the operator still has to pick
        a mode explicitly once.
        """
        result = self._resolve(
            mode_resolution_block,
            mode="",
            config_yaml="settings:\n  metrics_enabled: true\n",
        )
        assert result.returncode != 0
        assert "DIE:" in result.stderr

    def test_dies_on_invalid_mode_value(self, mode_resolution_block: str) -> None:
        result = self._resolve(
            mode_resolution_block, mode="container", config_yaml=None
        )
        assert result.returncode != 0
        assert "DIE:" in result.stderr

    def test_explicit_mode_resolves_with_no_existing_config(
        self, mode_resolution_block: str
    ) -> None:
        result = self._resolve(
            mode_resolution_block, mode="managed-host", config_yaml=None
        )
        assert result.returncode == 0
        assert "RESOLVED_MODE=managed-host" in result.stdout

    def test_bare_rerun_preserves_existing_mode(
        self, mode_resolution_block: str
    ) -> None:
        result = self._resolve(
            mode_resolution_block,
            mode="",
            config_yaml="deployment:\n  mode: appliance\n",
        )
        assert result.returncode == 0
        assert "RESOLVED_MODE=appliance" in result.stdout

    def test_explicit_mode_overrides_existing_mode(
        self, mode_resolution_block: str
    ) -> None:
        result = self._resolve(
            mode_resolution_block,
            mode="managed-host",
            config_yaml="deployment:\n  mode: appliance\n",
        )
        assert result.returncode == 0
        assert "RESOLVED_MODE=managed-host" in result.stdout
        assert "WARN: Switching deployment mode" in result.stderr


class TestModeSwitchTeardown(_ModeResolutionHarness):
    """issue #27/#30 acceptance: switching appliance -> managed-host tears
    down the outgoing mode's network artifacts; the reverse direction
    just re-provisions (no teardown needed, nothing appliance-specific
    exists yet in managed-host mode); same mode is a no-op.
    """

    def test_appliance_to_managed_host_switch_tears_down_artifacts(
        self, mode_resolution_block: str
    ) -> None:
        result = self._resolve(
            mode_resolution_block,
            mode="managed-host",
            config_yaml="deployment:\n  mode: appliance\n",
        )
        assert result.returncode == 0, result.stderr
        assert "Tearing down appliance network artifacts" in result.stdout
        assert "Stopping + disabling sp-rtk-base-net-provision.service" in (
            result.stdout
        )
        assert "Appliance network artifacts removed" in result.stdout

    def test_teardown_uses_ap_ssid_from_net_provision_yaml(
        self, mode_resolution_block: str
    ) -> None:
        """The switch must look up the *actual* configured AP SSID, not
        assume the default — a fleet that customised AP_SSID would
        otherwise leave the real profile behind.
        """
        result = self._resolve(
            mode_resolution_block,
            mode="managed-host",
            config_yaml="deployment:\n  mode: appliance\n",
            net_provision_yaml='ap_ssid: "custom-fleet-ap"\nap_password: "x"\n',
        )
        assert result.returncode == 0, result.stderr
        assert "custom-fleet-ap" in result.stdout

    def test_teardown_falls_back_to_default_ssid_when_net_provision_yaml_absent(
        self, mode_resolution_block: str
    ) -> None:
        result = self._resolve(
            mode_resolution_block,
            mode="managed-host",
            config_yaml="deployment:\n  mode: appliance\n",
            net_provision_yaml=None,
        )
        assert result.returncode == 0, result.stderr
        assert "sp-rtk-base-setup" in result.stdout

    def test_managed_host_to_appliance_switch_runs_no_teardown(
        self, mode_resolution_block: str
    ) -> None:
        result = self._resolve(
            mode_resolution_block,
            mode="appliance",
            config_yaml="deployment:\n  mode: managed-host\n",
        )
        assert result.returncode == 0, result.stderr
        assert "Tearing down appliance network artifacts" not in result.stdout
        assert "provisioning will run below" in result.stdout

    def test_same_mode_reinstall_runs_no_teardown_and_no_warning(
        self, mode_resolution_block: str
    ) -> None:
        result = self._resolve(
            mode_resolution_block,
            mode="appliance",
            config_yaml="deployment:\n  mode: appliance\n",
        )
        assert result.returncode == 0, result.stderr
        assert "Tearing down appliance network artifacts" not in result.stdout
        assert "WARN: Switching deployment mode" not in result.stderr


class TestApplianceOnlyGating:
    """Steps 8.2-8.6 (NetworkManager, setup-AP, polkit, net-provision unit)
    must run only in appliance mode — managed-host leaves the host's
    network stack untouched (issue #27 behavior matrix).
    """

    def test_network_takeover_steps_gated_behind_appliance_mode(
        self, install_script_text: str
    ) -> None:
        gate_idx = install_script_text.index('if [[ "$MODE" == "appliance" ]]; then')
        step86_idx = install_script_text.index("Step 8.6")
        step9_idx = install_script_text.index("# Step 9 — Final status")
        assert gate_idx < step86_idx < step9_idx, (
            "Steps 8.2-8.6 must all be nested inside the "
            '`if [[ "$MODE" == "appliance" ]]` block.'
        )
        gated_block = install_script_text[gate_idx:step9_idx]
        for needle in (
            "apt-get install -y --no-install-recommends network-manager",
            "nmcli connection add",
            "POLKIT_RULE_DEST",
            "NET_PROVISION_SYSTEMD_UNIT",
            "DNSMASQ_WILDCARD_CONF",
        ):
            assert needle in gated_block, (
                f"Expected {needle!r} inside the appliance-only gated block"
            )

    def test_managed_host_branch_skips_network_setup(
        self, install_script_text: str
    ) -> None:
        gate_idx = install_script_text.index('if [[ "$MODE" == "appliance" ]]; then')
        step9_idx = install_script_text.index("# Step 9 — Final status")
        block = install_script_text[gate_idx:step9_idx]
        assert re.search(r"\nelse\n\s*log .*managed-host", block), (
            "There must be an `else` branch inside the appliance gate that "
            "explicitly logs skipping network setup in managed-host mode."
        )

    def test_bluetooth_nudge_stays_unconditional(
        self, install_script_text: str
    ) -> None:
        """Step 7.6 (Bluetooth rfkill nudge) must run in *both* modes —
        it's outside the appliance-only gate entirely.
        """
        bt_idx = install_script_text.index("Step 7.6")
        gate_idx = install_script_text.index('if [[ "$MODE" == "appliance" ]]; then')
        assert bt_idx < gate_idx, (
            "Step 7.6 (Bluetooth) must appear before, and outside, the "
            "appliance-only gate so it always runs."
        )


class TestDeploymentModeWrittenToConfig:
    """Issue #29 acceptance: `deployment.mode` is written into config.yaml
    for the chosen/preserved mode. Covers both the fresh-install heredoc
    path and the existing-config sync path with an actual interpreter run
    (using this test suite's own venv), so a change to either doesn't
    silently break round-tripping through AppConfig/ConfigService.
    """

    @pytest.fixture(scope="class")
    def sync_snippet(self, install_script_text: str) -> str:
        marker = '"${VENV_DIR}/bin/python" - "$default_cfg" "$MODE" <<\'PY\'\n'
        start = install_script_text.index(marker) + len(marker)
        end = install_script_text.index("\nPY\n", start)
        return install_script_text[start:end]

    def test_sync_snippet_updates_mode_and_preserves_other_fields(
        self, sync_snippet: str, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "deployment:\n  mode: appliance\n"
            "settings:\n  metrics_enabled: false\n"
            "destinations:\n"
            "- name: foo\n  type: ntrip\n  config: {}\n"
        )
        venv_python = VENV_DIR_STANDIN / "bin" / "python"
        result = subprocess.run(
            [str(venv_python), "-c", sync_snippet, str(cfg_path), "managed-host"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = yaml.safe_load(cfg_path.read_text())
        assert data["deployment"]["mode"] == "managed-host"
        assert data["settings"]["metrics_enabled"] is False
        assert data["destinations"][0]["name"] == "foo"

    def test_config_sync_only_runs_when_mode_actually_changes(
        self, install_script_text: str
    ) -> None:
        """A bare re-run whose resolved MODE matches what's already on
        disk must not invoke the sync snippet at all — that would rewrite
        (and strip comments from) a config.yaml the installer promises to
        leave untouched.
        """
        assert 'elif [[ "$existing_mode" != "$MODE" ]]; then' in install_script_text

    def test_fresh_config_heredoc_interpolates_mode_directly(
        self, install_script_text: str
    ) -> None:
        """A brand-new config.yaml should get deployment.mode baked
        straight into the heredoc rather than immediately being rewritten
        by the sync snippet (which would strip the heredoc's comments)."""
        heredoc_idx = install_script_text.index('cat >"$default_cfg" <<YAML')
        next_yaml_end = install_script_text.index("\nYAML\n", heredoc_idx)
        heredoc = install_script_text[heredoc_idx:next_yaml_end]
        assert "mode: ${MODE}" in heredoc


class TestFinalStatusReflectsMode:
    """Issue #29 acceptance: final status output reflects the mode —
    managed-host output omits the net-provision/AP section.
    """

    def test_status_block_branches_on_mode(self, install_script_text: str) -> None:
        step9_idx = install_script_text.index("# Step 9 — Final status")
        block = install_script_text[step9_idx:]
        assert 'echo "  Deployment mode: ${MODE}"' in block
        assert 'if [[ "$MODE" == "appliance" ]]; then' in block
        assert "Network-provisioning service" in block
        assert "managed-host: host network stack untouched" in block


# ---------------------------------------------------------------------------
# uninstall.sh / upgrade.sh mode-tolerance (issue #27 / #31, T4)
# ---------------------------------------------------------------------------


class _UninstallModeReadHarness:
    """Executes the real deployment-mode-detection block out of
    uninstall.sh (added for #31) against a temp CONFIG_DIR, so the
    messaging/skip decision is exercised for real rather than
    reimplemented in Python. Mirrors the approach in
    :class:`_ModeResolutionHarness` for install.sh's Step 6.5.
    """

    @pytest.fixture(scope="class")
    def mode_read_block(self, uninstall_script_text: str) -> str:
        # Two non-adjacent spans: DEPLOYMENT_MODE detection, then (skipping
        # over ask()/the sp-rtk-base.service stop+unit-removal steps, which
        # need PURGE/KEEP_DATA/root and aren't part of what this harness
        # tests) the mode-branch that decides whether to tear down.
        detect_start = uninstall_script_text.index(
            "# Read deployment.mode (issue #27/#31)"
        )
        detect_end_marker = (
            '[[ -n "$parsed_mode" ]] && DEPLOYMENT_MODE="$parsed_mode"\nfi'
        )
        detect_end = uninstall_script_text.index(detect_end_marker, detect_start) + len(
            detect_end_marker
        )
        branch_start = uninstall_script_text.index(
            'echo "==> Detected deployment mode:'
        )
        branch_end_marker = 'teardown_appliance_network_artifacts "$AP_SSID"\nfi'
        branch_end = uninstall_script_text.index(branch_end_marker, branch_start) + len(
            branch_end_marker
        )
        return (
            uninstall_script_text[detect_start:detect_end]
            + "\n"
            + uninstall_script_text[branch_start:branch_end]
        )

    def _run(
        self, mode_read_block: str, config_yaml: str | None
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            if config_yaml is not None:
                (Path(td) / "config.yaml").write_text(config_yaml)
            script = f"""
set -euo pipefail
CONFIG_DIR="{td}"
AP_SSID="test-setup-ap"
venv_python="{VENV_DIR_STANDIN}/bin/python"
NET_PROVISION_SYSTEMD_UNIT="{td}/sp-rtk-base-net-provision.service"
POLKIT_RULE_DEST="{td}/10-sp-rtk-base-net-provision.rules"
DNSMASQ_WILDCARD_CONF="{td}/sp-rtk-base-wildcard.conf"
source "{NET_PROVISION_TEARDOWN_LIB}"
{mode_read_block}
"""
            return subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True
            )


class TestUninstallModeReadMessaging(_UninstallModeReadHarness):
    """Acceptance: uninstall.sh reads deployment.mode to give clear,
    mode-specific messaging — a managed-host install is a clean no-op
    with no spurious "removing network artifacts" noise, while an
    appliance install (or a pre-#28 config with no deployment section)
    keeps running the full, unchanged teardown.
    """

    def test_managed_host_skips_teardown_with_clean_message(
        self, mode_read_block: str
    ) -> None:
        result = self._run(
            mode_read_block, config_yaml="deployment:\n  mode: managed-host\n"
        )
        assert result.returncode == 0, result.stderr
        assert "Detected deployment mode: managed-host" in result.stdout
        assert "no network artifacts to remove" in result.stdout
        assert "Stopping + disabling sp-rtk-base-net-provision.service" not in (
            result.stdout
        )

    def test_appliance_mode_runs_full_teardown(self, mode_read_block: str) -> None:
        result = self._run(
            mode_read_block, config_yaml="deployment:\n  mode: appliance\n"
        )
        assert result.returncode == 0, result.stderr
        assert "Detected deployment mode: appliance" in result.stdout
        assert "Stopping + disabling sp-rtk-base-net-provision.service" in (
            result.stdout
        )

    def test_missing_config_defaults_to_appliance_teardown(
        self, mode_read_block: str
    ) -> None:
        """No config.yaml at all (fresh/never-installed edge case) must
        still run the teardown rather than silently skip it.
        """
        result = self._run(mode_read_block, config_yaml=None)
        assert result.returncode == 0, result.stderr
        assert "Detected deployment mode: appliance" in result.stdout
        assert "Stopping + disabling sp-rtk-base-net-provision.service" in (
            result.stdout
        )

    def test_pre_28_config_with_no_deployment_section_defaults_to_appliance(
        self, mode_read_block: str
    ) -> None:
        """A config.yaml written before issue #28 added the deployment
        section always came from a full appliance takeover install —
        must still get a full, unchanged cleanup.
        """
        result = self._run(
            mode_read_block, config_yaml="settings:\n  metrics_enabled: true\n"
        )
        assert result.returncode == 0, result.stderr
        assert "Detected deployment mode: appliance" in result.stdout
        assert "Stopping + disabling sp-rtk-base-net-provision.service" in (
            result.stdout
        )


class TestUpgradeShModeTolerance:
    """Acceptance: upgrade.sh never assumes appliance mode — it only
    restarts sp-rtk-base-net-provision.service if the unit is actually
    present, and never reads/writes deployment.mode itself (a version
    bump must not silently flip modes).
    """

    @pytest.fixture(scope="module")
    def upgrade_script_text(self) -> str:
        upgrade_script = REPO_ROOT / "deploy" / "upgrade.sh"
        assert upgrade_script.is_file(), f"upgrader not found: {upgrade_script}"
        return upgrade_script.read_text(encoding="utf-8")

    def test_net_provision_restart_gated_on_unit_presence(
        self, upgrade_script_text: str
    ) -> None:
        assert (
            "systemctl list-unit-files sp-rtk-base-net-provision.service"
            in upgrade_script_text
        )
        restart_idx = upgrade_script_text.index(
            "systemctl restart sp-rtk-base-net-provision.service"
        )
        preceding = upgrade_script_text[:restart_idx]
        guard_idx = preceding.rindex(
            "systemctl list-unit-files sp-rtk-base-net-provision.service"
        )
        guard = upgrade_script_text[guard_idx:restart_idx]
        assert "; then" in guard

    def test_never_references_deployment_mode(self, upgrade_script_text: str) -> None:
        """upgrade.sh must not read or write deployment.mode — it has no
        business deciding or changing which mode a host runs in.
        """
        assert "deployment" not in upgrade_script_text.lower()
        assert "MODE" not in upgrade_script_text

    def test_never_creates_appliance_only_artifacts(
        self, upgrade_script_text: str
    ) -> None:
        """A version bump must never provision network artifacts a
        managed-host install never had — upgrade.sh only ever
        stops/restarts the existing app service and, conditionally, the
        pre-existing net-provision unit.
        """
        for needle in ("nmcli connection add", "polkit", "NetworkManager"):
            assert needle not in upgrade_script_text
