"""E2E tests for profile pre-fill, Save-as, and the profile-relation
indicator (issues #66, #105, #106).

Extends the GPS page suite (``test_gps_profile_picker.py`` covers the
picker dropdown itself, ``test_gps_apply.py`` covers #65's out-of-sync
indicator + Apply). This suite covers picking a profile actually
writing into the form, plus the *second*, independent indicator:

- Selecting a compatible profile pre-fills the matrix, the data-link
  picker, and the "Hardware Section" display; the "modified from X"
  badge reads "Matches <name>" immediately after the pick (before any
  further edit) — the sparse-vs-dense matrix representation bug this
  guards against is covered at the unit level
  (``TestIsModifiedFromProfile`` in ``test_gps_config_helpers.py``).
- Editing the form after a pick flips the badge to "Modified from X".
- Save-as is available whenever the form is valid — no save control on
  the page is ever greyed (issue #106 dropped the old suppression for
  an unmodified copy of the selected profile; the dialog now explains
  that case with a note instead).
- Saving forks an independent custom profile carrying a
  ``forked_from`` provenance label; a name collision with a built-in
  is rejected outright, a collision with a custom profile offers an
  explicit overwrite.
- Rename/delete/export are customs-only, and act on whichever profile
  is currently selected in the dropdown (issue #105 moved them from a
  per-row icon set to a single icon trio beside the picker).
- Issue #106's three save entry points (the picker's persistent row,
  the action row's "Save as new profile…", the post-apply prompt) all
  open the same dialog; the in-place "Save profile" control is hidden
  — not greyed — for a built-in or no selection.

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

    # Issue #106: suppression is dropped — Save-as stays enabled even
    # on an unmodified copy of the selected profile. No save control on
    # this page is ever greyed.
    expect(page.locator(".save-as-btn")).to_be_enabled()


@pytest.mark.e2e
def test_editing_after_pick_flips_to_modified_and_save_as_stays_enabled(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Issue #106: Save-as is enabled the whole time — before *and* after
    the edit — while the "modified from X" badge is what actually tracks
    divergence from the picked profile."""
    _goto_gps_config(page, base_url)
    _select_builtin(page)
    expect(page.locator(".save-as-btn")).to_be_enabled()

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
    since both would be the same indicator. Also covers the post-apply
    save prompt (issue #106): offered because the form still differs from
    the selected profile."""
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
    expect(page.locator(".post-apply-save-row")).to_be_visible()


@pytest.mark.e2e
def test_no_post_apply_save_prompt_when_form_matches_selected_profile(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """AC: the post-apply prompt is offered only when the form differs
    from the selected profile or none is selected — an unmodified apply
    of the picked profile has nothing new worth offering to keep."""
    _goto_gps_config(page, base_url)
    _select_builtin(page)
    expect(page.locator(".modified-badge")).to_contain_text(
        f"Matches {BUILTIN_DISPLAY_NAME}"
    )

    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )
    expect(page.locator(".post-apply-save-row")).to_be_hidden()


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
def test_save_as_collision_with_a_builtin_is_rejected_outright(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """AC: "A collision with a built-in profile is rejected outright" —
    no overwrite offered, since that would let a custom profile shadow a
    reference configuration."""
    _goto_gps_config(page, base_url)
    page.locator(".save-as-btn").click()
    page.locator(".save-as-name input").fill(BUILTIN_NAME)
    page.locator(".save-as-confirm-btn").click()

    expect(page.locator(".save-as-error")).to_be_visible(timeout=5_000)
    expect(page.locator(".save-as-error")).to_contain_text(BUILTIN_NAME)
    expect(page.locator(".save-as-error")).to_contain_text("built-in")
    expect(page.locator(".save-as-overwrite-btn")).to_be_hidden()
    # Rejected, not overwritten — the dialog stays open.
    expect(page.locator(".save-as-name")).to_be_visible()


@pytest.mark.e2e
def test_save_as_collision_with_a_custom_offers_an_explicit_overwrite(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    cleanup_profiles: list[str],
) -> None:
    """AC: "A collision with a custom profile offers an explicit
    overwrite behind a confirm" — the error names the promise, and the
    revealed "Overwrite it" button actually replaces its saved content
    rather than silently failing or duplicating."""
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
    # 1074 has no live data on any port (see ``_goto_gps_config``'s
    # sibling docstrings) — a reliable off-to-on edit to verify below.
    page.locator(".rtcm-cell-1074-UART1").click()

    page.locator(".save-as-btn").click()
    page.locator(".save-as-name input").fill("e2e-collision")
    page.locator(".save-as-confirm-btn").click()

    expect(page.locator(".save-as-error")).to_be_visible(timeout=5_000)
    expect(page.locator(".save-as-error")).to_contain_text("e2e-collision")
    overwrite_btn = page.locator(".save-as-overwrite-btn")
    expect(overwrite_btn).to_be_visible()

    overwrite_btn.click()
    expect(page.locator(".save-as-name")).to_be_hidden()  # dialog closed

    resp = httpx.get(f"{api_base_url}/api/profiles/e2e-collision", timeout=5.0)
    assert resp.status_code == 200, resp.text
    matrix = resp.json()["profile"]["rtcm_stream"]["matrix"]
    assert matrix["1074"]["UART1"] is True

    # Overwritten in place, not duplicated — still exactly one option.
    page.locator(".profile-picker").click()
    expect(page.locator(".profile-option-e2e-collision")).to_have_count(1)


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


# ---------------------------------------------------------------------------
# Issue #106 — three entry points into one dialog, the in-place "Save
# profile" update, the live-derived slug, the unmodified note, and the
# picker's empty-state guidance.
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_picker_row_entry_point_opens_the_save_as_dialog(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """AC: "All three entry points open the same dialog" — entry point 1,
    the persistent row at the bottom of the picker."""
    _goto_gps_config(page, base_url)
    expect(page.locator(".save-as-picker-row")).to_be_visible()
    page.locator(".save-as-picker-row").click()
    expect(page.locator(".save-as-name")).to_be_visible(timeout=5_000)


@pytest.mark.e2e
def test_action_row_entry_point_opens_the_save_as_dialog(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Entry point 2: "Save as new profile…" in the action row. Every
    save control names "profile"."""
    _goto_gps_config(page, base_url)
    expect(page.locator(".save-as-btn")).to_contain_text("Save as new profile")
    page.locator(".save-as-btn").click()
    expect(page.locator(".save-as-name")).to_be_visible(timeout=5_000)


@pytest.mark.e2e
def test_post_apply_prompt_entry_point_opens_the_save_as_dialog(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Entry point 3: the inline prompt in the post-apply result panel."""
    _goto_gps_config(page, base_url)
    page.locator(".rtcm-cell-1077-UART1").click()  # diverge from live, so it's offered

    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )
    expect(page.locator(".post-apply-save-btn")).to_be_visible()
    page.locator(".post-apply-save-btn").click()
    expect(page.locator(".save-as-name")).to_be_visible(timeout=5_000)


@pytest.mark.e2e
def test_apply_versus_save_caption_is_present(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """AC: "the action row carries the Apply-versus-Save caption"."""
    _goto_gps_config(page, base_url)
    caption = page.locator(".apply-save-caption")
    expect(caption).to_be_visible()
    expect(caption).to_contain_text("Apply writes to the receiver")
    expect(caption).to_contain_text("Save stores the form as a profile")


@pytest.mark.e2e
def test_save_as_dialog_shows_the_derived_slug_live(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """AC: "The dialog shows the derived slug live as the operator types
    a display name"."""
    _goto_gps_config(page, base_url)
    page.locator(".save-as-btn").click()
    name_input = page.locator(".save-as-name input")
    name_input.fill("My Rooftop Base")
    expect(page.locator(".save-as-slug")).to_contain_text(
        "my-rooftop-base", timeout=5_000
    )


@pytest.mark.e2e
def test_save_as_dialog_shows_unmodified_note_instead_of_blocking(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """AC: "Opening the dialog on an unmodified copy shows an explanatory
    note instead of blocking" — the note names the source profile, and
    the dialog is still fully usable (Create stays enabled)."""
    _goto_gps_config(page, base_url)
    _select_builtin(page)

    page.locator(".save-as-btn").click()
    note = page.locator(".save-as-unmodified")
    expect(note).to_be_visible(timeout=5_000)
    expect(note).to_contain_text(BUILTIN_DISPLAY_NAME)
    expect(page.locator(".save-as-confirm-btn")).to_be_enabled()
    page.keyboard.press("Escape")

    # Diverge, then reopen — the note goes away, there's nothing to explain.
    page.locator(".rtcm-cell-1077-UART1").click()
    page.locator(".save-as-btn").click()
    expect(page.locator(".save-as-unmodified")).to_be_hidden(timeout=5_000)


@pytest.mark.e2e
def test_save_profile_hidden_not_greyed_for_builtin_or_no_selection(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    cleanup_profiles: list[str],
) -> None:
    """AC: "`Save profile` is hidden, not greyed, when a built-in or
    nothing is selected"."""
    custom = {
        "name": "e2e-save-profile-visibility",
        "version": 1,
        "hardware": "any",
        "data_link_port": ["UART1"],
        "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
    }
    resp = httpx.post(f"{api_base_url}/api/profiles", json=custom, timeout=5.0)
    assert resp.status_code in (201, 409), resp.text
    cleanup_profiles.append("e2e-save-profile-visibility")

    _goto_gps_config(page, base_url)
    save_profile_btn = page.locator(".save-profile-btn")
    expect(save_profile_btn).to_be_hidden()  # nothing selected

    _select_builtin(page)
    expect(save_profile_btn).to_be_hidden()  # built-in selected

    _select_profile_option(page, "e2e-save-profile-visibility")
    expect(save_profile_btn).to_be_visible()  # custom selected


@pytest.mark.e2e
def test_save_profile_updates_the_selected_custom_in_place(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    cleanup_profiles: list[str],
) -> None:
    """AC: "`Save profile` updates the selected custom in place behind a
    confirm, without asking for a name" — same slug afterwards, new
    content, and the "modified from X" badge flips back to "Matches"."""
    custom = {
        "name": "e2e-save-profile-inplace",
        "version": 1,
        "hardware": "any",
        "data_link_port": ["UART1"],
        "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
    }
    resp = httpx.post(f"{api_base_url}/api/profiles", json=custom, timeout=5.0)
    assert resp.status_code in (201, 409), resp.text
    cleanup_profiles.append("e2e-save-profile-inplace")

    _goto_gps_config(page, base_url)
    _select_profile_option(page, "e2e-save-profile-inplace")
    # Wait for the pick to fully settle before editing — same race
    # ``_select_builtin`` guards against.
    expect(page.locator(".modified-badge")).to_contain_text(
        "Matches e2e-save-profile-inplace", timeout=10_000
    )
    page.locator(".rtcm-cell-1077-UART1").click()
    expect(page.locator(".modified-badge")).to_contain_text(
        "Modified from e2e-save-profile-inplace"
    )

    page.locator(".save-profile-btn").click()
    expect(page.locator(".save-profile-confirm-btn")).to_be_visible(timeout=5_000)
    page.locator(".save-profile-confirm-btn").click()

    expect(page.locator(".save-profile-confirm-btn")).to_be_hidden(timeout=5_000)
    expect(page.locator(".modified-badge")).to_contain_text(
        "Matches e2e-save-profile-inplace"
    )

    resp = httpx.get(
        f"{api_base_url}/api/profiles/e2e-save-profile-inplace", timeout=5.0
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["profile"]["rtcm_stream"]["matrix"]["1077"]["UART1"] is True

    # Still exactly one option — an in-place update, not a fork.
    page.locator(".profile-picker").click()
    expect(page.locator(".profile-option-e2e-save-profile-inplace")).to_have_count(1)


@pytest.mark.e2e
def test_picker_shows_guidance_when_no_custom_profiles_exist(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    cleanup_profiles: list[str],
) -> None:
    """AC: "The picker shows guidance when no custom profiles exist"."""
    _goto_gps_config(page, base_url)
    expect(page.locator(".no-customs-hint")).to_be_visible(timeout=10_000)

    custom = {
        "name": "e2e-empty-state-guard",
        "version": 1,
        "hardware": "any",
        "data_link_port": ["UART1"],
        "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
    }
    resp = httpx.post(f"{api_base_url}/api/profiles", json=custom, timeout=5.0)
    assert resp.status_code in (201, 409), resp.text
    cleanup_profiles.append("e2e-empty-state-guard")

    page.reload()
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )
    expect(page.locator(".no-customs-hint")).to_be_hidden(timeout=10_000)
