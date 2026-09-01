"""E2E tests for profile pre-fill, Save-as, and the profile-relation
indicator (issues #66, #105).

Extends the GPS page suite (``test_gps_profile_picker.py`` covers the
picker dropdown itself, ``test_gps_apply.py`` covers #65's out-of-sync
indicator + Apply). This suite covers picking a profile actually
writing into the form, plus the *second*, independent indicator:

- Selecting a compatible profile pre-fills the matrix, the data-link
  picker, and the "Hardware Section" display; the "modified from X"
  badge reads "Matches <name>" immediately after the pick (before any
  further edit) — the sparse-vs-dense matrix representation bug this
  guards against is covered at the unit level
  (``TestIsModifiedFromProfile``/``TestSaveAsEnabled`` in
  ``test_gps_config_helpers.py``).
- Editing the form after a pick flips the badge to "Modified from X"
  and re-enables Save-as.
- Save-as is available with no profile selected (the bare-capture
  usage path) and suppressed only while a selected profile still
  exactly equals the form.
- Saving forks an independent custom profile carrying a
  ``forked_from`` provenance label; a name collision surfaces inline
  rather than silently overwriting.
- Rename/delete/export are customs-only, and act on whichever profile
  is currently selected in the dropdown (issue #105 moved them from a
  per-row icon set to a single icon trio beside the picker).

Locators target the stable CSS class hooks the page renders (see
``ui/pages/gps_config.py``), matching the existing suite's convention.
The dropdown's options only exist in the DOM while the popup is open,
so selecting a profile always opens ``.profile-picker`` first.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

BUILTIN_NAME = "ublox-f9p-base-standard"
#: The built-in's ``display_name`` (issue #95) — provenance labels
#: (picker option, "Matches"/"Modified from", "Forked from") render
#: this, not the slug ``BUILTIN_NAME``.
BUILTIN_DISPLAY_NAME = "u-blox F9P — Base Station (Standard)"


def _delete_profile(api_base_url: str, name: str) -> None:
    try:
        httpx.delete(f"{api_base_url}/api/profiles/{name}", timeout=5.0)
    except Exception:
        pass


@pytest.fixture()
def cleanup_profiles(api_base_url: str) -> Iterator[list[str]]:
    """Collects custom-profile names a test creates via the UI, deleting
    them on teardown regardless of pass/fail — the API-side counterpart to
    ``custom_incompatible_profile`` in ``test_gps_profile_picker.py``."""
    created: list[str] = []
    try:
        yield created
    finally:
        for name in created:
            _delete_profile(api_base_url, name)


def _goto_gps_config(page: Page, base_url: str) -> None:
    """Navigate to the page and wait for the initial live-seed to settle.

    ``connected_gps`` connects the fake driver *before* this page ever
    mounts, so the form's first load doesn't happen synchronously —
    it's the page's own one-shot, 100ms-deferred "already connected"
    timer (see ``_on_page_load``/``ui.timer`` in ``gps_config.py``)
    that calls ``_load_receiver_config_form()``. Interacting with the
    picker before that fires would have this reseed clobber the pick
    a moment later, so wait for a live-seeded cell first — the fake
    driver's default 1005/UART1 is always on.
    """
    page.goto(f"{base_url}/gps-config")
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )
    expect(page.locator(".rtcm-cell-1005-UART1")).to_have_text("✓", timeout=10_000)


def _select_profile_option(page: Page, slug: str) -> None:
    """Open the picker dropdown and click the option identified by *slug*.

    The option list only mounts in the DOM while the dropdown is open
    (it's a Quasar ``q-menu``), so every pick opens ``.profile-picker``
    first.
    """
    expect(page.locator(".profile-picker")).to_be_visible(timeout=10_000)
    page.locator(".profile-picker").click()
    option = page.locator(f".profile-option-{slug}")
    expect(option).to_be_visible(timeout=10_000)
    option.click()


def _select_builtin(page: Page) -> None:
    """Pick the built-in profile and wait for the pick to fully settle.

    ``_select_profile`` re-renders several sections over the NiceGUI
    websocket round-trip; without waiting for the last of them
    ("modified from X" only appears once ``_on_form_changed`` has run),
    a next action can land before the pick has actually taken effect
    client-side.
    """
    _select_profile_option(page, BUILTIN_NAME)
    expect(page.locator(".modified-badge")).to_contain_text(
        f"Matches {BUILTIN_DISPLAY_NAME}", timeout=10_000
    )


@pytest.mark.e2e
def test_selecting_profile_prefills_matrix_and_matches_immediately(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """The built-in's matrix (1074 on both UARTs) and data-link ports
    (UART1+UART2) land in the form, and "modified from X" reads "Matches"
    before any further edit — proving the comparison survives the
    profile's sparse-matrix vs. the form's dense-matrix representation."""
    _goto_gps_config(page, base_url)
    _select_builtin(page)

    expect(page.locator(".rtcm-cell-1074-UART1")).to_have_text("✓", timeout=10_000)
    expect(page.locator(".rtcm-cell-1074-UART2")).to_have_text("✓")
    expect(page.locator(".rtcm-cell-1074-USB")).to_have_text("-")

    # Quasar's checkbox drives state via `aria-checked` on the component
    # root, not the hidden native `<input>`'s `checked` property.
    expect(page.locator(".data-link-checkbox-UART1")).to_have_attribute(
        "aria-checked", "true"
    )
    expect(page.locator(".data-link-checkbox-UART2")).to_have_attribute(
        "aria-checked", "true"
    )

    modified_badge = page.locator(".modified-badge")
    expect(modified_badge).to_be_visible()
    expect(modified_badge).to_contain_text(f"Matches {BUILTIN_DISPLAY_NAME}")

    expect(page.locator(".save-as-btn")).to_be_disabled()


@pytest.mark.e2e
def test_editing_after_pick_flips_to_modified_and_reenables_save_as(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """The T4 fix this ticket exists for: an edit after a successful pick
    must not take Save-as away — quite the opposite, it's what re-enables
    it."""
    _goto_gps_config(page, base_url)
    _select_builtin(page)
    expect(page.locator(".save-as-btn")).to_be_disabled()

    page.locator(".rtcm-cell-1077-UART1").click()

    modified_badge = page.locator(".modified-badge")
    expect(modified_badge).to_contain_text(f"Modified from {BUILTIN_DISPLAY_NAME}")
    expect(page.locator(".save-as-btn")).to_be_enabled()


@pytest.mark.e2e
def test_save_as_stays_enabled_after_a_real_successful_apply(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """AC: "Save-as remains enabled after a successful apply when the form
    still differs from its source profile." Exercises this through the
    actual Apply button/endpoint, not just the pure-helper unit test — the
    approved prototype's bug (gating Save-as off the *receiver* comparison)
    would clear "modified from X" the moment Apply clears "out of sync",
    since both would be the same indicator."""
    _goto_gps_config(page, base_url)
    _select_builtin(page)
    page.locator(".rtcm-cell-1077-UART1").click()

    modified_badge = page.locator(".modified-badge")
    expect(modified_badge).to_contain_text(f"Modified from {BUILTIN_DISPLAY_NAME}")

    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )

    # Apply cleared "out of sync" (implicit — that's #65) but must leave
    # "modified from X" and Save-as exactly as they were.
    expect(modified_badge).to_contain_text(f"Modified from {BUILTIN_DISPLAY_NAME}")
    expect(page.locator(".save-as-btn")).to_be_enabled()


@pytest.mark.e2e
def test_save_as_enabled_with_no_profile_selected(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Usage path 3: connect, pick nothing, Save-as is still available."""
    _goto_gps_config(page, base_url)
    expect(page.locator(".modified-badge")).to_be_hidden()
    expect(page.locator(".save-as-btn")).to_be_enabled()


@pytest.mark.e2e
def test_save_as_forks_a_custom_profile_with_provenance(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    cleanup_profiles: list[str],
) -> None:
    """Saving while a profile is selected forks an independent custom
    profile carrying a "forked from" label — auto-suggested name
    sanitized to the store's filesystem-safe slug charset."""
    _goto_gps_config(page, base_url)
    _select_builtin(page)
    page.locator(".rtcm-cell-1077-UART1").click()  # diverge, so Save-as is live

    page.locator(".save-as-btn").click()
    name_input = page.locator(".save-as-name input")
    expect(name_input).to_have_value(f"{BUILTIN_NAME}-copy")
    expect(page.locator(".save-as-from")).to_contain_text(
        f"Forked from: {BUILTIN_DISPLAY_NAME}"
    )

    forked_name = f"{BUILTIN_NAME}-copy"
    cleanup_profiles.append(forked_name)
    page.locator(".save-as-confirm-btn").click()

    expect(page.locator(".save-as-name")).to_be_hidden()  # dialog closed

    resp = httpx.get(f"{api_base_url}/api/profiles/{forked_name}", timeout=5.0)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile"]["forked_from"] == BUILTIN_NAME
    assert body["profile"]["hardware"] == "ZED-F9P"
    assert body["is_builtin"] is False
    # The edited cell made it into the saved document.
    assert body["profile"]["rtcm_stream"]["matrix"]["1077"]["UART1"] is True

    page.locator(".profile-picker").click()
    expect(page.locator(f".profile-option-{forked_name}")).to_be_visible(timeout=10_000)


@pytest.mark.e2e
def test_save_as_bare_capture_has_no_forked_from(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    cleanup_profiles: list[str],
) -> None:
    """No profile selected -> the saved document is a bare capture, named
    from the connected receiver's identity, with no provenance label."""
    _goto_gps_config(page, base_url)

    page.locator(".save-as-btn").click()
    expect(page.locator(".save-as-name input")).to_have_value("zed-f9p-captured")
    expect(page.locator(".save-as-from")).to_be_hidden()

    cleanup_profiles.append("zed-f9p-captured")
    page.locator(".save-as-confirm-btn").click()
    expect(page.locator(".save-as-name")).to_be_hidden()

    resp = httpx.get(f"{api_base_url}/api/profiles/zed-f9p-captured", timeout=5.0)
    assert resp.status_code == 200, resp.text
    assert resp.json()["profile"]["forked_from"] is None


@pytest.mark.e2e
def test_save_as_name_collision_is_rejected_with_a_clear_error(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    cleanup_profiles: list[str],
) -> None:
    existing = {
        "name": "e2e-collision",
        "version": 1,
        "hardware": "any",
        "data_link_port": ["UART1"],
        "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
    }
    resp = httpx.post(f"{api_base_url}/api/profiles", json=existing, timeout=5.0)
    assert resp.status_code in (201, 409), resp.text
    cleanup_profiles.append("e2e-collision")

    _goto_gps_config(page, base_url)
    page.locator(".save-as-btn").click()
    page.locator(".save-as-name input").fill("e2e-collision")
    page.locator(".save-as-confirm-btn").click()

    expect(page.locator(".save-as-error")).to_be_visible(timeout=5_000)
    expect(page.locator(".save-as-error")).to_contain_text("e2e-collision")
    # Rejected, not overwritten — the dialog stays open.
    expect(page.locator(".save-as-name")).to_be_visible()


@pytest.mark.e2e
def test_rename_delete_export_are_customs_only(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    cleanup_profiles: list[str],
) -> None:
    """Rename/delete/export (issue #105: a single icon trio beside the
    picker, acting on whichever profile is currently selected) only
    show up once a *custom* profile is the current selection."""
    custom = {
        "name": "e2e-custom-actions",
        "version": 1,
        "hardware": "any",
        "data_link_port": ["UART1"],
        "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
    }
    resp = httpx.post(f"{api_base_url}/api/profiles", json=custom, timeout=5.0)
    assert resp.status_code in (201, 409), resp.text
    cleanup_profiles.append("e2e-custom-actions")

    _goto_gps_config(page, base_url)

    # Nothing selected yet — the icons stay hidden.
    expect(page.locator(".profile-rename-icon")).to_be_hidden()
    expect(page.locator(".profile-delete-icon")).to_be_hidden()
    expect(page.locator(".profile-export-icon")).to_be_hidden()

    _select_profile_option(page, BUILTIN_NAME)
    expect(page.locator(".profile-rename-icon")).to_be_hidden()
    expect(page.locator(".profile-delete-icon")).to_be_hidden()
    expect(page.locator(".profile-export-icon")).to_be_hidden()

    _select_profile_option(page, "e2e-custom-actions")
    expect(page.locator(".profile-rename-icon")).to_be_visible()
    expect(page.locator(".profile-delete-icon")).to_be_visible()
    expect(page.locator(".profile-export-icon")).to_be_visible()


@pytest.mark.e2e
def test_rename_then_delete_custom_profile(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    cleanup_profiles: list[str],
) -> None:
    """Rename edits the display name only — the slug (and so the option's
    CSS identity, ``.profile-option-e2e-rename-me``) never changes; only
    the rendered label updates."""
    custom = {
        "name": "e2e-rename-me",
        "version": 1,
        "hardware": "any",
        "data_link_port": ["UART1"],
        "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
    }
    resp = httpx.post(f"{api_base_url}/api/profiles", json=custom, timeout=5.0)
    assert resp.status_code in (201, 409), resp.text
    cleanup_profiles.append("e2e-rename-me")

    _goto_gps_config(page, base_url)

    _select_profile_option(page, "e2e-rename-me")
    page.locator(".profile-rename-icon").click()

    # No display_name set yet — the dialog falls back to the slug.
    expect(page.locator(".rename-name input")).to_have_value("e2e-rename-me")
    page.locator(".rename-name input").fill("e2e Renamed")
    page.locator(".rename-confirm-btn").click()

    # Same option (same slug) — only the visible label changed.
    page.locator(".profile-picker").click()
    option = page.locator(".profile-option-e2e-rename-me")
    expect(option).to_be_visible(timeout=10_000)
    expect(option).to_contain_text("e2e Renamed")
    page.keyboard.press("Escape")

    page.locator(".profile-delete-icon").click()
    page.locator(".delete-confirm-btn").click()
    expect(page.locator(".profile-delete-icon")).to_be_hidden(timeout=10_000)

    page.locator(".profile-picker").click()
    expect(page.locator(".profile-option-e2e-rename-me")).to_have_count(0)
