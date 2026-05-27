"""End-to-end button-click test for the Input Source page.

Covers the single button on ``/input``:

- **Save Input Config** with the default TCP source: the click should
  produce the ``Input source saved ✓`` Quasar toast, and a subsequent
  ``GET /api/config/export`` must echo the host/port we typed into
  the form.

The form's other source types (``serial`` and ``bluetooth``) rely on
host-level dependencies (pyserial port enumeration, BlueZ over DBus)
that the headless test runner can't satisfy, so we focus the e2e
coverage on TCP — the only path that's fully deterministic.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
import yaml
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_save_tcp_input_button_persists_to_config(
    page: Page,
    base_url: str,
    api_base_url: str,
) -> None:
    """Type a TCP host/port, click **Save Input Config**, verify persistence.

    Steps:
    1. Navigate to ``/input`` — the default source is TCP, so the
       host/port text inputs are visible immediately.
    2. Fill the Host and Port fields with values that are distinct
       from the defaults (``127.0.0.1`` / ``5015``).
    3. Click **Save Input Config**.
    4. Expect the success toast.
    5. Pull ``GET /api/config/export`` (YAML download) and verify the
       new host/port land in the ``input.config`` block.

    The config-service writes synchronously, so the export call
    immediately after the toast reliably reflects the new state — no
    extra ``wait_for`` needed.
    """
    page.goto(f"{base_url}/input")
    expect(page.locator("text=Input Source").first).to_be_visible(timeout=15_000)

    # Distinct sentinel values so we don't false-positive on the
    # defaults that the form populates on first render.
    sentinel_host = "203.0.113.42"
    sentinel_port = "9942"

    host_input = page.get_by_label("Host")
    port_input = page.get_by_label("Port")
    expect(host_input).to_be_visible(timeout=10_000)
    expect(port_input).to_be_visible(timeout=10_000)

    host_input.fill(sentinel_host)
    port_input.fill(sentinel_port)

    page.get_by_role("button", name="Save Input Config").click()

    # The handler emits the literal string "Input source saved ✓".
    # Matching the leading words is enough — the trailing emoji can
    # be brittle across browsers' text rendering.
    expect(page.locator("text=Input source saved").first).to_be_visible(timeout=10_000)

    # Verify persistence via the YAML export endpoint.  ``ConfigService``
    # writes through to disk synchronously inside ``save_input_config``,
    # so the export call below sees the new state.
    resp = page.request.get(f"{api_base_url}/api/config/export")
    assert resp.ok, resp.text()
    parsed: object = yaml.safe_load(resp.text())
    assert isinstance(parsed, dict), (
        f"config/export YAML root not a mapping: {parsed!r}"
    )
    config = cast(dict[str, Any], parsed)

    input_block = config.get("input")
    assert isinstance(input_block, dict), (
        f"expected config.input dict in exported YAML; got {input_block!r}"
    )
    assert input_block.get("source") == "tcp", (
        f"expected source='tcp', got {input_block.get('source')!r}"
    )
    inner = input_block.get("config")
    assert isinstance(inner, dict), f"expected config.input.config dict; got {inner!r}"
    assert inner.get("host") == sentinel_host, (
        f"host not persisted: expected {sentinel_host!r}, got {inner.get('host')!r}"
    )
    # The form gathers values as strings; the round-trip preserves
    # them as strings, which is acceptable for the YAML config.
    assert str(inner.get("port")) == sentinel_port, (
        f"port not persisted: expected {sentinel_port!r}, got {inner.get('port')!r}"
    )
