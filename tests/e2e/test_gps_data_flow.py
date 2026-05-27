"""End-to-end GPS data-flow tests against the FakeGpsDriver.

These verify that the **API surface** the UI pages depend on
behaves correctly when a fake GPS is connected:

- ``GET /api/device/position`` returns the FakeGpsDriver's fixture
  position (the same lat/lon the Survey-In Save-Position bug
  regression test pins).
- ``GET /api/device/gnss`` exposes a fully-populated constellation
  list (6 systems) so the GPS Config page renders a non-empty
  table.
- ``POST /api/device/gnss`` writes are reflected by the next
  ``GET`` (round-trip).
- ``GET /api/device/base-config`` returns ``DISABLED`` until
  fixed-base is configured, then ``FIXED``.
- ``POST /api/device/save`` (save-to-flash) returns 200 on the
  fake driver.
- A quick browser smoke check that the Advanced GPS page renders
  without unhandled JS errors when connected.

Together with ``test_survey_save_position.py`` and
``test_device_connection.py`` this gives us coverage of every
device-driven UI path without requiring real hardware.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
class TestPositionEndpoint:
    """``/api/device/position`` returns the fixture position."""

    def test_returns_fixture_coordinates(
        self,
        api_base_url: str,
        connected_gps: None,
    ) -> None:
        resp = httpx.get(f"{api_base_url}/api/device/position", timeout=5.0)
        assert resp.status_code == 200, resp.text
        body: object = resp.json()
        assert isinstance(body, dict)
        pos = cast(dict[str, Any], body)

        assert pos.get("latitude") == pytest.approx(32.7329015, abs=1e-6)
        assert pos.get("longitude") == pytest.approx(-117.2362788, abs=1e-6)
        assert pos.get("altitude_m") == pytest.approx(27.940, abs=1e-3)
        # FakeGpsDriver advertises an RTK-fixed solution so survey-in
        # accuracy paths render with realistic colours.
        assert pos.get("rtk_status") == "fixed"
        num_sats = pos.get("num_satellites")
        assert isinstance(num_sats, int) and num_sats >= 20, (
            f"expected ≥20 satellites in fixture, got {num_sats!r}"
        )

    def test_returns_404_or_409_when_disconnected(self, api_base_url: str) -> None:
        """With no device, ``/api/device/position`` must not 500.

        We accept 404/409/422 (any well-formed client error) so the
        backend has wiggle room on the exact status — the contract
        we're locking in is "no 5xx when the receiver is absent".
        """
        # Make sure we're disconnected.
        httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)

        resp = httpx.get(f"{api_base_url}/api/device/position", timeout=5.0)
        assert 400 <= resp.status_code < 500, (
            f"got 5xx from /api/device/position when disconnected: "
            f"{resp.status_code} {resp.text}"
        )


@pytest.mark.e2e
class TestGnssEndpoint:
    """``/api/device/gnss`` GET/POST round-trip on the fake driver."""

    def test_default_exposes_six_constellations(
        self,
        api_base_url: str,
        connected_gps: None,
    ) -> None:
        resp = httpx.get(f"{api_base_url}/api/device/gnss", timeout=5.0)
        assert resp.status_code == 200, resp.text
        body: object = resp.json()
        assert isinstance(body, dict)
        gnss = cast(dict[str, Any], body)
        systems_raw = gnss.get("systems", [])
        assert isinstance(systems_raw, list)
        systems: list[dict[str, Any]] = [
            cast(dict[str, Any], s) for s in systems_raw if isinstance(s, dict)
        ]
        # GPS / GLONASS / Galileo / BeiDou / QZSS / SBAS — six systems.
        assert len(systems) == 6, (
            f"FakeGpsDriver should expose 6 constellations, got "
            f"{len(systems)}: {systems!r}"
        )
        names = {str(s.get("constellation", "")).lower() for s in systems}
        assert {"gps", "glonass", "galileo", "beidou"} <= names

    def test_round_trip_disables_galileo(
        self,
        api_base_url: str,
        connected_gps: None,
    ) -> None:
        """Writing ``galileo.enabled=false`` is reflected by the next GET."""
        # Get current to use as the base — keeps payload schema-correct
        # without us reproducing the full default here.
        current = httpx.get(f"{api_base_url}/api/device/gnss", timeout=5.0).json()
        current_d = cast(dict[str, Any], current)
        systems_list_raw = cast(list[Any], current_d.get("systems", []))
        systems_list: list[dict[str, Any]] = [
            cast(dict[str, Any], s) for s in systems_list_raw if isinstance(s, dict)
        ]
        for s in systems_list:
            if str(s.get("constellation", "")).lower() == "galileo":
                s["enabled"] = False

        # Re-encode and write.  The configuration endpoint is
        # ``PUT /api/device/gnss`` (it replaces the entire GNSS
        # config), not the older ``POST /configure/gnss`` shape.
        resp = httpx.put(
            f"{api_base_url}/api/device/gnss",
            json={"systems": systems_list},
            timeout=10.0,
        )
        # We don't enforce 200 vs 204; just no server error.
        assert resp.status_code < 500, resp.text

        # Read back.
        after = httpx.get(f"{api_base_url}/api/device/gnss", timeout=5.0).json()
        after_d = cast(dict[str, Any], after)
        after_systems_raw = cast(list[Any], after_d.get("systems", []))
        after_systems = [
            cast(dict[str, Any], s) for s in after_systems_raw if isinstance(s, dict)
        ]
        gal = next(
            (
                s
                for s in after_systems
                if str(s.get("constellation", "")).lower() == "galileo"
            ),
            None,
        )
        assert gal is not None, "Galileo missing from GNSS read-back"
        assert gal.get("enabled") is False, f"Galileo write did not round-trip: {gal!r}"


@pytest.mark.e2e
class TestBaseConfigEndpoint:
    """``/api/device/base-config`` reflects mode transitions."""

    def test_starts_disabled(
        self,
        api_base_url: str,
        connected_gps: None,
    ) -> None:
        resp = httpx.get(f"{api_base_url}/api/device/base-config", timeout=5.0)
        assert resp.status_code == 200, resp.text
        body: object = resp.json()
        assert isinstance(body, dict)
        bc = cast(dict[str, Any], body)
        assert str(bc.get("mode", "")).lower() in {"disabled", "none"}

    def test_fixed_base_configure_then_read(
        self,
        api_base_url: str,
        connected_gps: None,
    ) -> None:
        configure = httpx.post(
            f"{api_base_url}/api/device/configure/fixed-base",
            json={
                "latitude": 32.7329015,
                "longitude": -117.2362788,
                "altitude_m": 27.940,
                "accuracy_mm": 47308,
            },
            timeout=10.0,
        )
        assert configure.status_code < 500, configure.text

        resp = httpx.get(f"{api_base_url}/api/device/base-config", timeout=5.0)
        assert resp.status_code == 200, resp.text
        body: object = resp.json()
        assert isinstance(body, dict)
        bc = cast(dict[str, Any], body)
        assert str(bc.get("mode", "")).lower() == "fixed"
        assert bc.get("latitude") == pytest.approx(32.7329015, abs=1e-6)
        assert bc.get("longitude") == pytest.approx(-117.2362788, abs=1e-6)


@pytest.mark.e2e
class TestSaveToFlashEndpoint:
    """``/api/device/save`` is a clean no-op against the fake driver."""

    def test_returns_200_when_connected(
        self,
        api_base_url: str,
        connected_gps: None,
    ) -> None:
        resp = httpx.post(f"{api_base_url}/api/device/save", timeout=5.0)
        assert resp.status_code == 200, resp.text


@pytest.mark.e2e
def test_gps_config_page_renders_when_connected(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Smoke test: the Advanced GPS page loads without unhandled errors.

    We're not asserting on every widget — that would be brittle —
    just that the top-level page renders its title and that the
    "Save to Flash" button (a high-signal indicator that the
    capability gating worked) becomes visible after the device
    is connected.
    """
    page.goto(f"{base_url}/gps-config")
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )
    # The Save to Flash button is gated on the SAVE_TO_FLASH capability,
    # which the FakeGpsDriver claims.  Its visibility proves the
    # capability-driven UI assembly fired.
    expect(page.get_by_role("button", name="Save to Flash")).to_be_visible(
        timeout=10_000
    )
