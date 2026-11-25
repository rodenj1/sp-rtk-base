"""Tests for the Bluetooth scan-duration knobs on the Input page module.

These guard the constants used by the Input page's "Scan for Devices"
dropdown and the underlying ``_discover_bluetooth_devices`` helper.
The page itself (NiceGUI handlers) is excluded from coverage, but the
constants and the helper's parameter wiring are pure-Python and worth
locking down so a future refactor can't silently regress the default
scan duration back to a too-short value.
"""

# pyright: reportPrivateUsage=false
# We intentionally exercise the module-private helper
# ``_discover_bluetooth_devices`` from the tests in this file — the
# helper is a thin, pure-Python wrapper used by the page handler and
# has no public alias to bind a test against.

from __future__ import annotations

import pytest
from sp_rtk_base_relay.core.bluetooth_manager import DiscoveredDevice

from sp_rtk_base.ui.pages import input as input_page


class TestBluetoothScanDurationConstants:
    """Module-level constants powering the Scan duration dropdown."""

    def test_default_is_at_least_twenty_seconds(self) -> None:
        """Default scan must comfortably cover slow-advertising devices."""
        assert input_page.DEFAULT_BT_SCAN_DURATION_SECONDS >= 20

    def test_default_is_in_dropdown_options(self) -> None:
        """The default must be one of the offered presets."""
        assert (
            input_page.DEFAULT_BT_SCAN_DURATION_SECONDS
            in input_page.BT_SCAN_DURATIONS_SECONDS
        )

    def test_dropdown_options_are_sorted_ascending(self) -> None:
        """Operators expect Scan duration choices to grow shortest→longest."""
        opts = input_page.BT_SCAN_DURATIONS_SECONDS
        assert opts == sorted(opts)

    def test_dropdown_options_all_positive_ints(self) -> None:
        """A non-positive scan duration would silently no-op the BT scan."""
        for value in input_page.BT_SCAN_DURATIONS_SECONDS:
            assert isinstance(value, int)
            assert value > 0

    def test_dropdown_offers_long_presets(self) -> None:
        """Operators must be able to opt into 30 / 45 / 60 s scans.

        The user explicitly asked for these three longer presets so a
        slow-advertising receiver can be discovered without code edits.
        """
        opts = input_page.BT_SCAN_DURATIONS_SECONDS
        for required in (30, 45, 60):
            assert required in opts, (
                f"Scan duration preset {required}s missing from dropdown"
            )


class TestDiscoverBluetoothDevicesScanSeconds:
    """The discovery helper must honour and clamp the scan duration."""

    def test_default_scan_seconds_matches_default_constant(self) -> None:
        """Default arg equals the module-level default."""
        import inspect

        sig = inspect.signature(input_page._discover_bluetooth_devices)
        assert (
            sig.parameters["scan_seconds"].default
            == input_page.DEFAULT_BT_SCAN_DURATION_SECONDS
        )

    def test_non_positive_scan_seconds_is_clamped_to_default(self) -> None:
        """A 0/negative value scans for the default — never ``sleep(0)``."""
        mgr = _ScanMgr([])

        input_page._discover_bluetooth_devices(mgr, scan_seconds=0)
        input_page._discover_bluetooth_devices(mgr, scan_seconds=-5)
        input_page._discover_bluetooth_devices(mgr, scan_seconds=45)

        default = input_page.DEFAULT_BT_SCAN_DURATION_SECONDS
        assert mgr.scans == [default, default, 45]


class TestDiscoverBluetoothDevicesListing:
    """The scan lists what the manager's public ``discover_devices`` found."""

    def test_named_paired_first_then_named_then_nameless(self) -> None:
        mgr = _ScanMgr(
            [
                DiscoveredDevice("00:00:00:00:00:01", None, paired=True),
                DiscoveredDevice("00:00:00:00:00:02", "zeta", paired=False),
                DiscoveredDevice("00:00:00:00:00:03", "Alpha", paired=False),
                DiscoveredDevice("00:00:00:00:00:04", "RTK_BASE_TST", paired=True),
            ]
        )

        found = input_page._discover_bluetooth_devices(mgr, scan_seconds=20)

        assert [d.mac_address[-2:] for d in found] == ["04", "03", "02", "01"]

    def test_a_failed_scan_is_raised_for_the_page_to_report(self) -> None:
        class _BrokenMgr:
            def discover_devices(self, scan_seconds: float) -> list[DiscoveredDevice]:
                raise RuntimeError("BlueZ is not running")

        with pytest.raises(RuntimeError, match="BlueZ is not running"):
            input_page._discover_bluetooth_devices(_BrokenMgr(), scan_seconds=20)


class _ScanMgr:
    """Stands in for the Relay's ``BluetoothManager`` scan."""

    def __init__(self, devices: list[DiscoveredDevice]) -> None:
        self._devices = devices
        self.scans: list[float] = []

    def discover_devices(self, scan_seconds: float) -> list[DiscoveredDevice]:
        self.scans.append(scan_seconds)
        return list(self._devices)
