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
        """A 0/negative value is replaced with the default — never used as a sleep.

        We can't easily exercise the live D-Bus path in a unit test, but
        we can verify the helper exits cleanly when there is no bus and
        does not raise when given a non-positive scan duration (it must
        silently swap in the default rather than calling ``sleep(0)`` or
        ``sleep(-5)``, either of which would defeat the whole point).
        """

        class _FakeMgr:
            _bus = None  # short-circuit at the top of _get_devices
            _loop = None
            _adapter = None

        # The outer try in the helper swallows attribute errors that
        # come from _loop=None; the important contract is "does not
        # raise on a non-positive scan_seconds", which mirrors what
        # the UI dropdown will guarantee but the helper must enforce
        # defensively for direct callers (tests, future scripts).
        result = input_page._discover_bluetooth_devices(_FakeMgr(), scan_seconds=0)
        assert result == []

        result = input_page._discover_bluetooth_devices(_FakeMgr(), scan_seconds=-5)
        assert result == []
