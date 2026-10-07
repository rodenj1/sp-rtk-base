"""PROTOTYPE, throwaway: Update UX variants for the Settings page.

Question (rtk_development "Update UX prototype"): how should Update look and
behave to the operator?

Three variants of the Settings page, switchable via ``?variant=A|B|C`` on the
existing ``/settings`` route, plus ``?state=<scenario>`` to push each variant
through every Update state. All data is mocked; nothing here touches PyPI,
GitHub, systemd or the request file. Lives on the ``prototype/update-ux``
branch only; never merge.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any

from nicegui import ui

RUNNING_APP = "0.9.0"
RUNNING_RELAY = "0.6.2"
TARGET_APP = "0.10.1"
TARGET_RELAY = "0.7.0"
INSTALL_CMD = (
    "curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/"
    "deploy/install.sh | sudo bash"
)

# ---------------------------------------------------------------------------
# Mock Release notes: every release after the running one, newest first.
# Pre-release sections fold under the stable release they led to.
# ---------------------------------------------------------------------------
APP_NOTES: list[dict[str, Any]] = [
    {
        "version": "0.10.1",
        "date": "2026-10-02",
        "md": "### Fixed\n- Survey-in no longer stalls when the receiver "
        "drops Fixed for one epoch.\n- Release note with markup shows as "
        "text: <b>not bold</b> <script>alert(1)</script>",
    },
    {
        "version": "0.10.0",
        "date": "2026-09-20",
        "folded": ["0.10.0-beta.1", "0.10.0-beta.2"],
        "md": "### Added\n- **Update** from the web UI.\n- Event log keeps "
        "its own live relay event stream.\n### Changed\n- Settings "
        "groups versions and Update in one place.",
    },
    {"version": "0.9.1", "date": "2026-09-05", "md": None},  # no notes
]
RELAY_NOTES: list[dict[str, Any]] = [
    {
        "version": "0.7.0",
        "date": "2026-09-18",
        "md": "### Added\n- NTRIP caster sends a sourcetable on reconnect.",
    },
    {
        "version": "0.6.3",
        "date": "2026-08-30",
        "md": "### Fixed\n- RTCM 1230 passes through unchanged.",
    },
]


@dataclass
class Scenario:
    """One mocked Update state, as the page would read it."""

    label: str
    available: bool = True
    relay_running: bool = False
    check: str = "ok"  # ok | checking | failed
    notes: str = "ok"  # ok | failed
    block: str | None = None  # reason Update can't be pressed, or None
    block_cmd: bool = False  # show the install.sh command with the block
    phase: str | None = None  # status.json phase while updating
    outcome: str | None = None  # last outcome shown on Settings
    outcome_kind: str = "info"  # positive | warning | negative | info
    banner: str | None = None  # page-wide banner
    banner_kind: str = "info"
    drift: bool = False  # running app needs newer Host setup than the host
    failed_here: bool = False  # the offered release failed here before
    python_note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


SCENARIOS: dict[str, Scenario] = {
    "available": Scenario("Available update, Relay idle"),
    "relay-running": Scenario("Available update, Relay running", relay_running=True),
    "up-to-date": Scenario("Up to date", available=False),
    "checking": Scenario("Check now in progress", check="checking"),
    "check-failed": Scenario("Couldn't check (last good result shown)", check="failed"),
    "notes-failed": Scenario("Notes couldn't be loaded (PyPI fine)", notes="failed"),
    "python-too-old": Scenario(
        "Newest release needs newer Python",
        python_note="0.11.0 needs Python 3.12; this base runs 3.11. "
        "Offering 0.10.1, the newest release that runs here.",
    ),
    "survey": Scenario(
        "Refused: Survey-in running",
        block="A Survey-in is running. Update once it has finished.",
    ),
    "console": Scenario(
        "Refused: Console link connected",
        block="A Console link is connected. Disconnect it to update.",
    ),
    "host-missing": Scenario(
        "Host setup missing",
        block="Update needs a one-time setup on this host. Run this on the "
        "base, then come back:",
        block_cmd=True,
    ),
    "turned-off": Scenario(
        "Turned off on this host", block="Update is turned off on this host."
    ),
    "host-older": Scenario(
        "Release needs newer Host setup",
        block="This release needs a one-time host setup step. Run this on "
        "the base, then come back:",
        block_cmd=True,
    ),
    "host-unknown": Scenario(
        "Couldn't read host requirements",
        block="Couldn't check this release's host requirements. Check again.",
    ),
    "drift": Scenario(
        "Running app needs newer Host setup", available=False, drift=True
    ),
    "requested": Scenario(
        "Updating: requested", phase="requested", banner="Updating to 0.10.1…"
    ),
    "installing": Scenario(
        "Updating: installing", phase="installing", banner="Updating to 0.10.1…"
    ),
    "restarting": Scenario(
        "Updating: restarting (page reconnects)",
        phase="restarting",
        banner="Restarting into 0.10.1. This page reconnects by itself.",
    ),
    "verifying": Scenario(
        "Updating: verifying", phase="verifying", banner="Checking 0.10.1 started…"
    ),
    "didnt-start": Scenario(
        "Update didn't start (30 s)",
        outcome="Update didn't start: the host didn't pick up the request "
        "within 30 s. Nothing changed.",
        outcome_kind="warning",
    ),
    "newer-appeared": Scenario(
        "A newer release appeared",
        outcome="Update not started: a newer release appeared since you "
        "checked. Check again and read its notes.",
        outcome_kind="warning",
    ),
    "disk": Scenario(
        "Refused: not enough disk space",
        outcome="Update to 0.10.1 not started: not enough disk space. Nothing changed.",
        outcome_kind="warning",
    ),
    "done": Scenario(
        "Done: now on X",
        available=False,
        outcome="Updated 0.9.0 → 0.10.1 on 7 Oct 14:02.",
        outcome_kind="positive",
        banner="Now on 0.10.1.",
        banner_kind="positive",
    ),
    "rolled-back": Scenario(
        "Failed, rolled back",
        failed_here=True,
        outcome="Update to 0.10.1 failed to start; rolled back to 0.9.0 "
        "on 7 Oct 14:04.",
        outcome_kind="warning",
        banner="Update to 0.10.1 failed to start; still on 0.9.0.",
        banner_kind="warning",
    ),
    "double-fail": Scenario(
        "Failed, rollback failed too",
        available=False,
        outcome="Update to 0.10.1 failed and the rollback to 0.9.0 failed "
        "too. On the base run: sudo deploy/upgrade.sh 0.9.0, and see "
        "journalctl -u sp-rtk-base-update.",
        outcome_kind="negative",
        banner="Update failed and could not roll back. See Settings.",
        banner_kind="negative",
    ),
}

PHASES = ["requested", "resolving", "installing", "restarting", "verifying", "done"]
PHASE_TEXT = {
    "requested": "Waiting for the host",
    "resolving": "Checking the release",
    "installing": "Installing",
    "restarting": "Restarting",
    "verifying": "Checking it started",
    "done": "Done",
}
VARIANTS = {
    "A": "Inline in Version card",
    "B": "Notes-first Update card",
    "C": "Strip + review dialog",
}

KIND_COLOR = {
    "positive": "#1b5e20",
    "warning": "#7a5200",
    "negative": "#7f1d1d",
    "info": "#1e3a5f",
}


def scenario(state: str | None) -> tuple[str, Scenario]:
    key = state if state in SCENARIOS else "available"
    return key, SCENARIOS[key]


# ---------------------------------------------------------------------------
# Shared bits (small on purpose; layouts are per variant)
# ---------------------------------------------------------------------------
def header_badge(s: Scenario) -> None:
    """Badge in the layout header. Shown in every Host setup state."""
    if s.phase:
        ui.badge("Updating…", color="orange").classes("q-ml-sm")
    elif s.available:
        with ui.link(target="/settings").classes("no-underline q-ml-sm"):
            ui.badge(f"Update {TARGET_APP}", color="teal").props("rounded")


def page_banner(s: Scenario) -> None:
    if not s.banner:
        return
    with (
        ui.row()
        .classes("w-full items-center q-pa-sm rounded-borders q-mb-md")
        .style(f"background:{KIND_COLOR[s.banner_kind]}")
    ):
        if s.phase:
            ui.spinner(size="sm", color="white")
        ui.label(s.banner).classes("text-white")
        ui.space()
        if not s.phase:
            ui.button(icon="close").props("flat round dense color=white")


def safe_md(md: str) -> None:
    """Markdown with raw HTML escaped: notes show as text, never markup."""
    ui.markdown(html.escape(md, quote=False))


def notes_body(s: Scenario, entries: list[dict[str, Any]], repo: str) -> None:
    if s.notes == "failed":
        ui.label(
            f"{repo} notes couldn't be loaded (GitHub didn't answer). "
            "Update still works."
        ).classes("text-grey-5 text-italic")
        return
    for e in entries:
        title = f"{repo} {e['version']} · {e['date']}"
        ui.label(title).classes("text-subtitle2 text-white q-mt-sm")
        if e.get("folded"):
            ui.label("Includes " + ", ".join(e["folded"])).classes(
                "text-caption text-grey-6"
            )
        if e["md"]:
            safe_md(e["md"])
        else:
            ui.label("No notes for this release.").classes("text-grey-6 text-italic")


def check_line(s: Scenario) -> None:
    with ui.row().classes("items-center q-gutter-sm"):
        if s.check == "checking":
            ui.spinner(size="xs")
            ui.label("Checking…").classes("text-grey-5 text-caption")
        elif s.check == "failed":
            ui.icon("cloud_off", color="amber").classes("text-caption")
            ui.label("Couldn't check (last checked 6 Oct 09:12)").classes(
                "text-amber text-caption"
            )
        else:
            ui.label("Last checked 7 Oct 09:12").classes("text-grey-6 text-caption")
        ui.button("Check now", icon="refresh").props(
            f"flat dense size=sm {'disable' if s.phase or s.check == 'checking' else ''}"
        )


def outcome_line(s: Scenario) -> None:
    if not s.outcome:
        return
    with (
        ui.row()
        .classes("w-full items-start q-pa-sm rounded-borders q-my-sm no-wrap")
        .style(
            f"border-left: 4px solid {KIND_COLOR[s.outcome_kind]}; background:#20203a"
        )
    ):
        ui.label(s.outcome).classes("text-grey-3")


def block_box(s: Scenario) -> None:
    if s.python_note:
        ui.label(s.python_note).classes("text-caption text-grey-5")
    if s.drift:
        ui.label(
            "This version needs a newer host setup than the host has. Some "
            "features may not work until you run:"
        ).classes("text-amber")
        ui.code(INSTALL_CMD, language="bash").classes("w-full")
    if s.block:
        ui.label(s.block).classes("text-amber")
        if s.block_cmd:
            ui.code(INSTALL_CMD, language="bash").classes("w-full")
    if s.failed_here and s.available:
        ui.label(f"{TARGET_APP} failed to start here on 7 Oct 14:04.").classes(
            "text-caption text-orange-4"
        )


def confirm_dialog(s: Scenario) -> ui.dialog:
    with ui.dialog() as dlg, ui.card().classes("q-pa-md").style("min-width: 360px"):
        ui.label(f"Update to {TARGET_APP}?").classes("text-h6 text-white")
        ui.label(
            f"SP-Base {RUNNING_APP} → {TARGET_APP}, Relay {RUNNING_RELAY} → "
            f"{TARGET_RELAY}. The base restarts; this page reconnects by itself."
        ).classes("text-grey-4")
        if s.relay_running:
            with ui.row().classes("items-center no-wrap q-mt-sm"):
                ui.icon("warning", color="amber")
                ui.label(
                    "The Relay is running. Corrections stop for about a minute "
                    "and resume on their own."
                ).classes("text-amber")
        with ui.row().classes("w-full justify-end q-mt-md"):
            ui.button("Cancel", on_click=dlg.close).props("flat")
            ui.button(
                "Update",
                icon="system_update",
                on_click=lambda: (
                    dlg.close(),
                    ui.notify("Prototype: no request written"),
                ),
            ).props("color=primary")
    return dlg


def update_button(s: Scenario, label: str = "Update") -> None:
    dlg = confirm_dialog(s)
    disabled = bool(s.block or s.phase or not s.available or s.check == "checking")
    ui.button(
        f"{label} to {TARGET_APP}", icon="system_update", on_click=dlg.open
    ).props(f"color=primary {'disable' if disabled else ''}")


def phase_progress(s: Scenario, vertical: bool = False) -> None:
    if not s.phase:
        return
    idx = PHASES.index(s.phase)
    if vertical:
        for i, p in enumerate(PHASES[:-1]):
            icon = (
                "check_circle"
                if i < idx
                else ("autorenew" if i == idx else "radio_button_unchecked")
            )
            color = "green" if i < idx else ("orange" if i == idx else "grey-7")
            with ui.row().classes("items-center"):
                ui.icon(icon, color=color)
                ui.label(PHASE_TEXT[p]).classes(
                    "text-white" if i <= idx else "text-grey-7"
                )
    else:
        ui.linear_progress(
            value=(idx + 0.5) / (len(PHASES) - 1), show_value=False
        ).props("color=orange")
        ui.label(
            f"{PHASE_TEXT[s.phase]}… (step {idx + 1} of {len(PHASES) - 1})"
        ).classes("text-grey-4 text-caption")


def version_rows(s: Scenario, arrows: bool) -> None:
    for name, cur, new in (
        ("SP-Base", RUNNING_APP, TARGET_APP),
        ("SP-Base Relay", RUNNING_RELAY, TARGET_RELAY),
    ):
        with ui.row().classes("w-full items-center q-py-xs"):
            ui.label(name).classes("text-grey-4").style("min-width: 140px")
            ui.label(cur).classes("text-white text-weight-medium")
            if arrows and s.available:
                ui.icon("arrow_forward", color="teal").classes("text-caption")
                ui.label(new).classes("text-teal text-weight-medium")


# ---------------------------------------------------------------------------
# Variant A: Update folded into the existing Version Information card
# ---------------------------------------------------------------------------
def variant_a(s: Scenario) -> None:
    with ui.card().classes("w-full q-pa-md q-mt-md"):
        with ui.row().classes("w-full items-center"):
            ui.label("Version & Update").classes("text-h6 text-white")
            ui.space()
            check_line(s)
        ui.separator()
        outcome_line(s)
        version_rows(s, arrows=True)
        if not s.available and not s.phase and not s.outcome:
            ui.label("Up to date.").classes("text-grey-5 q-mt-xs")
        block_box(s)
        phase_progress(s)
        if s.available:
            with ui.expansion("Release notes", icon="notes").classes("w-full q-mt-sm"):
                with ui.tabs().classes("text-white") as tabs:
                    t_app = ui.tab("SP-Base")
                    t_relay = ui.tab("Relay")
                with ui.tab_panels(tabs, value=t_app).classes("w-full bg-transparent"):
                    with ui.tab_panel(t_app):
                        notes_body(s, APP_NOTES, "SP-Base")
                    with ui.tab_panel(t_relay):
                        notes_body(s, RELAY_NOTES, "Relay")
            update_button(s)


# ---------------------------------------------------------------------------
# Variant B: its own card above everything else, notes open, side by side
# ---------------------------------------------------------------------------
def variant_b(s: Scenario) -> None:
    with ui.card().classes("w-full q-pa-md q-mb-md").style("border: 1px solid #2e7d6b"):
        if s.phase:
            ui.label(f"Updating to {TARGET_APP}").classes("text-h6 text-white")
            phase_progress(s, vertical=True)
            return
        if s.available:
            ui.label(f"Update available: SP-Base {TARGET_APP}").classes(
                "text-h6 text-white"
            )
            ui.label(
                f"You run {RUNNING_APP} with Relay {RUNNING_RELAY}. Update brings "
                f"Relay {TARGET_RELAY} too."
            ).classes("text-grey-4")
        else:
            ui.label(f"SP-Base {RUNNING_APP} is up to date").classes(
                "text-h6 text-white"
            )
        check_line(s)
        outcome_line(s)
        block_box(s)
        if s.available:
            with ui.row().classes("w-full q-mt-sm sp-metric-row no-wrap items-start"):
                with ui.column().classes("col"):
                    ui.label("What changed in SP-Base").classes(
                        "text-overline text-grey-5"
                    )
                    notes_body(s, APP_NOTES, "SP-Base")
                with ui.column().classes("col"):
                    ui.label("What changed in the Relay").classes(
                        "text-overline text-grey-5"
                    )
                    notes_body(s, RELAY_NOTES, "Relay")
            ui.separator().classes("q-my-sm")
            update_button(s, "Update now")


# ---------------------------------------------------------------------------
# Variant C: one-line strip; reading and confirming happen in a dialog
# ---------------------------------------------------------------------------
def variant_c(s: Scenario) -> None:
    with ui.dialog().props("maximized") as review, ui.card().classes("q-pa-lg"):
        with ui.row().classes("w-full items-center"):
            ui.label(f"Update to {TARGET_APP}").classes("text-h5 text-white")
            ui.space()
            ui.button(icon="close", on_click=review.close).props("flat round")
        with ui.stepper().props("vertical flat").classes("w-full bg-transparent") as st:
            with ui.step("1. Read what changed"):
                notes_body(s, APP_NOTES, "SP-Base")
                notes_body(s, RELAY_NOTES, "Relay")
                with ui.stepper_navigation():
                    ui.button("I've read it", on_click=st.next)
            with ui.step("2. Check the base is free"):
                if s.block:
                    block_box(s)
                else:
                    ui.label(
                        "No Survey-in running, no Console link connected."
                    ).classes("text-green-4")
                if s.relay_running:
                    ui.label(
                        "The Relay is running: corrections stop for about a minute "
                        "and resume on their own."
                    ).classes("text-amber")
                with ui.stepper_navigation():
                    ui.button("Back", on_click=st.previous).props("flat")
                    ui.button("Next", on_click=st.next).props(
                        "disable" if s.block else ""
                    )
            with ui.step("3. Update"):
                ui.label(
                    f"SP-Base {RUNNING_APP} → {TARGET_APP}, Relay {RUNNING_RELAY} → "
                    f"{TARGET_RELAY}. The base restarts; this page reconnects."
                ).classes("text-grey-4")
                with ui.stepper_navigation():
                    ui.button("Back", on_click=st.previous).props("flat")
                    ui.button(
                        "Update",
                        icon="system_update",
                        on_click=lambda: (
                            review.close(),
                            ui.notify("Prototype: no request written"),
                        ),
                    ).props("color=primary")

    with (
        ui.row()
        .classes("w-full items-center q-pa-md rounded-borders q-mb-md")
        .style("background:#1e2a3a")
    ):
        ui.icon("system_update", color="teal" if s.available else "grey-6")
        if s.phase:
            ui.label(f"Updating to {TARGET_APP}: {PHASE_TEXT[s.phase]}…").classes(
                "text-white"
            )
        elif s.available:
            ui.label(f"Update available: {RUNNING_APP} → {TARGET_APP}").classes(
                "text-white"
            )
        else:
            ui.label(f"Up to date ({RUNNING_APP})").classes("text-white")
        ui.space()
        check_line(s)
        if s.available and not s.phase:
            ui.button("Review & update", on_click=review.open).props("color=primary")
    outcome_line(s)
    if s.block_cmd or s.drift or s.python_note or s.failed_here:
        block_box(s)


def render(variant: str, s: Scenario) -> None:
    page_banner(s)
    {"A": variant_a, "B": variant_b, "C": variant_c}[variant](s)


# ---------------------------------------------------------------------------
# Floating switchers: variant (← →) and scenario picker
# ---------------------------------------------------------------------------
def switcher(variant: str, state: str) -> None:
    keys = list(VARIANTS)
    i = keys.index(variant)
    prev_v, next_v = keys[i - 1], keys[(i + 1) % len(keys)]

    def go(v: str, st: str) -> None:
        ui.navigate.to(f"/settings?variant={v}&state={st}")

    with (
        ui.row()
        .classes("items-center q-px-md q-py-xs no-wrap")
        .style(
            "position:fixed; bottom:44px; left:50%; transform:translateX(-50%);"
            "background:#fafafa; color:#111; border-radius:999px; z-index:9999;"
            "box-shadow:0 4px 16px rgba(0,0,0,.5)"
        )
    ):
        ui.button(icon="chevron_left", on_click=lambda: go(prev_v, state)).props(
            "flat round dense color=black"
        )
        ui.label(f"{variant} ({VARIANTS[variant]})").classes("text-weight-bold")
        ui.button(icon="chevron_right", on_click=lambda: go(next_v, state)).props(
            "flat round dense color=black"
        )
        ui.select(
            {k: v.label for k, v in SCENARIOS.items()},
            value=state,
            on_change=lambda e: go(variant, e.value),
        ).props("dense options-dense outlined bg-color=white").style("min-width:260px")

    ui.keyboard(
        on_key=lambda e: (
            go(prev_v, state)
            if e.key.arrow_left and e.action.keydown
            else go(next_v, state)
            if e.key.arrow_right and e.action.keydown
            else None
        ),
        ignore=["input", "select", "button", "textarea"],
    )
