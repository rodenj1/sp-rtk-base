"""PROTOTYPE, throwaway: the Correction source option on the Survey page.

Question (rodenj1/rtk_development#21): how should picking, creating and
verifying a Correction source, and watching correction state during a
Corrected survey-in, look next to the existing start / cancel /
auto-commit flow and the tighter limits?

Three structurally different variants are mounted on the real ``/survey``
route, switchable with ``?variant=A|B|C`` and the floating bottom bar:

* **A: Mode switch.** A Plain / Corrected toggle inside the Survey-In card.
* **B: Guided stepper.** Type, then source, then limits, then run, with an event log.
* **C: Separate card.** A "Correction Sources" card of its own, a single
  "Corrections from" select on the Survey-In card, and a sticky status strip.

Everything below is a stub held in memory. Nothing touches the receiver,
the Relay or settings. The "Prototype controls" panel injects the events
the real thing would see (Verification outcomes, corrections dropping,
Float-only, stall aborts). Only active when ``SP_RTK_BASE_PROTOTYPE=1``.
Lives only on the ``prototype/correction-source-survey-page`` branch.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownLambdaType=false

from __future__ import annotations

import json
import math
import os
import random
from dataclasses import asdict, dataclass, field

from nicegui import ui

VARIANTS = {"A": "Mode switch", "B": "Guided stepper", "C": "Separate card"}

STAGES = ["connect", "caster", "auth", "mountpoint", "data"]

PLAIN_LIMITS = (120, 50000)  # seconds, mm
CORRECTED_LIMITS = (300, 50)
STALL_ABORT_S = 600
GREEN_TTL_S = 30


def prototype_enabled() -> bool:
    return os.environ.get("SP_RTK_BASE_PROTOTYPE") == "1"


# ----------------------------------------------------------------------
# Stub model
# ----------------------------------------------------------------------


@dataclass
class Source:
    name: str
    caster: str
    port: int
    mountpoint: str
    username: str
    version: str = "2.0"
    tls: bool = False
    has_password: bool = True


@dataclass
class Verification:
    verdict: str  # green / red
    stages: list[tuple[str, str, str]]  # (stage, status, code)
    age_s: float = 0.0


@dataclass
class Sim:
    sources: list[Source] = field(
        default_factory=lambda: [
            Source("RTK2go KY-LEX", "rtk2go.com", 2101, "KY_LEX", "me@example.com"),
            Source("Bench 2RTKNTRIP", "192.168.1.20", 2101, "MP1", "bench"),
        ]
    )
    selected: str | None = "RTK2go KY-LEX"
    verification: Verification | None = None
    mode: str = "plain"  # plain / corrected
    duration_s: int = PLAIN_LIMITS[0]
    accuracy_mm: int = PLAIN_LIMITS[1]
    # run state
    outcome: str = "idle"  # idle running completed cancelled aborted
    abort_reason: str | None = None
    relay: str = "-"  # connecting connected reconnecting -
    relay_error: str | None = None
    wall_s: float = 0.0
    observation_s: float = 0.0
    stall_s: float = 0.0
    reported_acc_mm: float = 0.0
    rtk_status: str = "none"
    correction_age_s: float | None = None
    # injected conditions
    corrections_flowing: bool = True
    float_only: bool = False
    since_corrections_s: float = 0.0
    speed: int = 1
    events: list[str] = field(default_factory=lambda: list[str]())

    # -- helpers ---------------------------------------------------------
    def source(self) -> Source | None:
        return next((s for s in self.sources if s.name == self.selected), None)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.duration_s, self.accuracy_mm = (
            CORRECTED_LIMITS if mode == "corrected" else PLAIN_LIMITS
        )
        self.verification = None

    def log(self, msg: str) -> None:
        self.events.append(f"[{int(self.wall_s):>4}s] {msg}")

    def verify(self, kind: str) -> None:
        if kind == "green":
            st = [(s, "passed", "") for s in STAGES]
            self.verification = Verification("green", st)
        elif kind == "warning":
            st = [(s, "passed", "") for s in STAGES[:-1]]
            st.append(("data", "warning", "no_reference_position"))
            self.verification = Verification("green", st)
        elif kind == "red_auth":
            st = [("connect", "passed", ""), ("caster", "passed", "")]
            st += [("auth", "failed", "401 Unauthorized")]
            st += [("mountpoint", "skipped", ""), ("data", "skipped", "")]
            self.verification = Verification("red", st)
        elif kind == "red_silent":
            st = [(s, "passed", "") for s in STAGES[:-1]]
            st.append(("data", "failed", "silent"))
            self.verification = Verification("red", st)

    def start(self) -> str | None:
        """Return a refusal message, or None when started."""
        if self.mode == "corrected" and self.source() is None:
            return "Pick or create a Correction source first."
        self.outcome = "running"
        self.abort_reason = None
        self.relay_error = None
        self.wall_s = self.observation_s = self.stall_s = 0.0
        self.since_corrections_s = 0.0
        self.reported_acc_mm = 0.0
        self.rtk_status = "none"
        self.correction_age_s = None
        self.corrections_flowing = True
        self.float_only = False
        self.events = []
        if self.mode == "corrected":
            self.relay = "connecting"
            self.log(f"Receiver set to rover; connecting to {self.selected}")
        else:
            self.relay = "-"
            self.log("Receiver set to rover; application averaging started")
        return None

    def cancel(self) -> None:
        if self.outcome == "running":
            self.outcome = "cancelled"
            self.relay = "-"
            self.log("Cancelled; receiver left in rover mode")

    def _abort(self, reason: str) -> None:
        self.outcome = "aborted"
        self.abort_reason = reason
        self.relay = "-"
        self.log(f"Aborted: {reason}; nothing committed, receiver in rover mode")

    def tick(self, dt: float) -> None:
        if self.outcome != "running":
            if self.verification is not None:
                self.verification.age_s += dt
                if self.verification.age_s > GREEN_TTL_S:
                    self.verification = None
            return
        self.wall_s += dt
        if self.mode == "plain":
            self.observation_s += dt
            self.rtk_status = "none"
            self.reported_acc_mm = max(900.0, 2500 * math.exp(-self.wall_s / 300))
        else:
            self._tick_corrected(dt)
        cap = max(3600, 3 * self.duration_s)
        done = (
            self.observation_s >= self.duration_s
            and self.reported_acc_mm <= self.accuracy_mm
        )
        if done:
            self.outcome = "completed"
            self.relay = "-"
            self.log(
                f"Complete: fixed base committed (±{self.reported_acc_mm:.0f} mm) "
                "and saved to flash"
            )
        elif self.stall_s >= STALL_ABORT_S:
            fresh = self.correction_age_s is not None and self.correction_age_s < 10
            self._abort("no_fixed" if fresh else "no_corrections")
        elif self.wall_s >= cap:
            self._abort("accuracy_not_reached")

    def _tick_corrected(self, dt: float) -> None:
        if self.relay == "connecting" and self.wall_s >= 2:
            self.relay = "connected"
            self.log("Correction source connected (200 OK)")
        if self.relay != "connected" and self.relay != "reconnecting":
            self.stall_s += dt
            return
        if self.corrections_flowing:
            self.since_corrections_s += dt
            self.correction_age_s = round(random.uniform(0.4, 1.6), 1)
        else:
            self.correction_age_s = (self.correction_age_s or 0) + dt
        prev = self.rtk_status
        age = self.correction_age_s or 999
        if age > 30:
            self.rtk_status = "none"
        elif not self.corrections_flowing and age > 5:
            self.rtk_status = "float"
        elif self.since_corrections_s < 6:
            self.rtk_status = "none"
        elif self.float_only or self.since_corrections_s < 18:
            self.rtk_status = "float"
        else:
            self.rtk_status = "fixed"
        if prev != self.rtk_status:
            self.log(f"RTK {prev} -> {self.rtk_status}")
        if self.rtk_status == "fixed":
            self.observation_s += dt
            self.stall_s = 0.0
            self.reported_acc_mm = max(
                14.0 + random.uniform(-1, 1), 120 * math.exp(-self.observation_s / 60)
            )
        else:
            self.stall_s += dt

    def drop_corrections(self) -> None:
        self.corrections_flowing = False
        self.relay = "reconnecting"
        self.relay_error = "data timeout (30 s without bytes)"
        self.log("Correction stream stalled; Relay reconnecting")

    def restore_corrections(self) -> None:
        self.corrections_flowing = True
        self.since_corrections_s = 0.0
        self.relay = "connected"
        self.relay_error = None
        self.log("Correction source reconnected")

    def snapshot(self) -> str:
        d = asdict(self)
        d.pop("sources")
        d["events"] = d["events"][-5:]
        return json.dumps(d, indent=1, default=str)


# ----------------------------------------------------------------------
# Shared small pieces (not layouts)
# ----------------------------------------------------------------------

_STATUS_COLOR = {
    "passed": "positive",
    "failed": "negative",
    "warning": "warning",
    "skipped": "grey-7",
}
_RTK_COLOR = {"fixed": "positive", "float": "warning", "none": "grey-7"}


def stage_chips(v: Verification | None) -> None:
    if v is None:
        ui.label("Not verified").classes("text-caption text-grey-5")
        return
    with ui.row().classes("gap-1 items-center"):
        verdict = "Green" if v.verdict == "green" else "Red"
        ui.chip(
            f"{verdict} · {max(0, GREEN_TTL_S - int(v.age_s))} s"
            if v.verdict == "green"
            else verdict,
            color="positive" if v.verdict == "green" else "negative",
        ).props("dense square")
        for stage, status, code in v.stages:
            text = stage + (f": {code}" if code else "")
            ui.chip(text, color=_STATUS_COLOR[status]).props("dense outline")


def source_editor(sim: Sim, existing: Source | None, on_save: object) -> None:
    """Dialog probing which fields the operator sees (feeds the storage ticket)."""
    with ui.dialog() as dlg, ui.card().classes("q-pa-md").style("min-width: 420px"):
        ui.label(
            "Edit Correction source" if existing else "New Correction source"
        ).classes("text-h6 text-white")
        name = ui.input("Name", value=existing.name if existing else "").classes(
            "w-full"
        )
        with ui.row().classes("w-full gap-2"):
            caster = ui.input(
                "Caster", value=existing.caster if existing else ""
            ).classes("col-grow")
            port = ui.number("Port", value=existing.port if existing else 2101).classes(
                "w-24"
            )
        mount = ui.input(
            "Mountpoint", value=existing.mountpoint if existing else ""
        ).classes("w-full")
        with ui.row().classes("w-full gap-2"):
            user = ui.input(
                "Username", value=existing.username if existing else ""
            ).classes("col-grow")
            ui.input(
                "Password",
                password=True,
                placeholder="•••••• (saved)"
                if existing and existing.has_password
                else "",
            ).classes("col-grow")
        with ui.row().classes("w-full gap-2 items-center"):
            version = ui.toggle({"1.0": "NTRIP v1", "2.0": "NTRIP v2"}, value="2.0")
            ui.checkbox("TLS").bind_enabled_from(version, "value", lambda v: v == "2.0")
        with ui.expansion("Advanced (defaults)").classes("w-full text-grey-5"):
            ui.label(
                "connection_timeout 15 s · data_timeout 30 s · retry 10 → 120 s x2"
            ).classes("text-caption")
        with ui.row().classes("gap-2 q-mt-md justify-end w-full"):
            ui.button("Cancel", on_click=dlg.close).props("flat")

            def _save() -> None:
                src = Source(
                    name.value or "Unnamed",
                    caster.value,
                    int(port.value or 2101),
                    mount.value,
                    user.value,
                    version.value,
                )
                if existing:
                    sim.sources[sim.sources.index(existing)] = src
                else:
                    sim.sources.append(src)
                sim.selected = src.name
                sim.verification = None
                dlg.close()
                on_save()  # type: ignore[operator]

            ui.button("Save", on_click=_save).props("color=primary")
    dlg.open()


def verify_menu(sim: Sim, after: object) -> None:
    """The 'Verify' button: real one runs ~5-15 s; here you pick the outcome."""
    with ui.button("Verify", icon="fact_check").props("outline color=info"):
        with ui.menu():
            for key, label in [
                ("green", "→ Green"),
                ("warning", "→ Green with Warning (no 1005/1006)"),
                ("red_auth", "→ Red at auth"),
                ("red_silent", "→ Red at data (silent)"),
            ]:
                ui.menu_item(
                    label,
                    on_click=lambda k=key: (sim.verify(k), after()),  # type: ignore[operator]
                )


def prototype_controls(sim: Sim) -> None:
    with ui.expansion("Prototype controls & state", icon="science").classes(
        "w-full q-mt-md bg-grey-9"
    ):
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button("Drop corrections", on_click=sim.drop_corrections).props(
                "dense outline"
            )
            ui.button("Restore corrections", on_click=sim.restore_corrections).props(
                "dense outline"
            )
            ui.button(
                "Float only",
                on_click=lambda: setattr(sim, "float_only", not sim.float_only),
            ).props("dense outline")
            ui.button(
                "Jump stall +9 min",
                on_click=lambda: setattr(sim, "stall_s", sim.stall_s + 540),
            ).props("dense outline")
            ui.toggle({1: "1x", 10: "10x", 60: "60x"}).bind_value(sim, "speed")

        @ui.refreshable
        def _state() -> None:
            ui.code(sim.snapshot(), language="json").classes("w-full text-caption")

        _state()
        ui.timer(1.0, _state.refresh)


def outcome_text(sim: Sim) -> str:
    if sim.outcome == "running":
        return "Surveying (corrected)" if sim.mode == "corrected" else "Surveying"
    return {
        "idle": "Idle",
        "completed": "Complete: fixed base committed and saved to flash",
        "cancelled": "Cancelled",
        "aborted": f"Aborted: {sim.abort_reason}",
    }[sim.outcome]


ABORT_COPY = {
    "no_corrections": "No corrections reached the receiver for 10 minutes.",
    "no_fixed": "Corrections arrived, but the receiver never held RTK Fixed for 10 minutes.",
    "accuracy_not_reached": "The accuracy limit was not reached before the time cap.",
}


# ----------------------------------------------------------------------
# Variant A: Plain / Corrected toggle inside the Survey-In card
# ----------------------------------------------------------------------


def variant_a(sim: Sim) -> None:
    with ui.card().classes("w-full q-pa-md q-mt-md"):
        ui.label("Survey-In").classes("text-h6 text-white")
        ui.separator()
        mode = ui.toggle(
            {"plain": "Plain", "corrected": "Corrected (cm-level)"}, value=sim.mode
        ).classes("q-mt-sm")

        source_row = ui.column().classes("w-full gap-1 q-mt-sm")
        with source_row:
            with ui.row().classes("w-full items-center gap-2"):
                sel = ui.select(
                    [s.name for s in sim.sources],
                    value=sim.selected,
                    label="Correction source",
                ).classes("col-grow")
                sel.bind_value(sim, "selected")
                verify_menu(sim, lambda: chips.refresh())
                ui.button(
                    icon="edit",
                    on_click=lambda: source_editor(sim, sim.source(), _resync),
                ).props("flat round").tooltip("Edit source")
                ui.button(
                    icon="add", on_click=lambda: source_editor(sim, None, _resync)
                ).props("flat round").tooltip("New source")

            @ui.refreshable
            def chips() -> None:
                stage_chips(sim.verification)

            chips()
            ui.label(
                "While corrected, the base sends no RTCM: the receiver is a rover."
            ).classes("text-caption text-warning")

        def _resync() -> None:
            sel.options = [s.name for s in sim.sources]
            sel.value = sim.selected
            sel.update()
            chips.refresh()

        with ui.row().classes("w-full gap-4 q-mt-sm"):
            dur = ui.number("Min duration (s of RTK Fixed)", min=60, max=86400).classes(
                "col-grow"
            )
            acc = ui.number("Accuracy limit (mm)").classes("col-grow")
            dur.bind_value(sim, "duration_s")
            acc.bind_value(sim, "accuracy_mm")

        def _mode_changed() -> None:
            sim.set_mode(mode.value)
            source_row.set_visibility(mode.value == "corrected")
            dur.props(
                f"label='{'Min duration (s of RTK Fixed)' if mode.value == 'corrected' else 'Min duration (s)'}'"
            )
            acc.props(f"min={10 if mode.value == 'corrected' else 1000}")
            chips.refresh()

        mode.on_value_change(_mode_changed)
        source_row.set_visibility(sim.mode == "corrected")

        with ui.row().classes("gap-2 q-mt-sm"):

            def _start() -> None:
                err = sim.start()
                if err:
                    ui.notify(err, type="warning")

            ui.button("Start Survey-In", icon="play_arrow", on_click=_start).props(
                "color=primary"
            ).bind_enabled_from(sim, "outcome", lambda o: o != "running")
            ui.button("Cancel Survey", icon="stop", on_click=sim.cancel).props(
                "color=negative outline"
            ).bind_visibility_from(sim, "outcome", lambda o: o == "running")

        @ui.refreshable
        def progress() -> None:
            if sim.outcome == "idle":
                return
            with ui.card().classes("w-full q-pa-sm q-mt-sm bg-grey-10"):
                ui.label(outcome_text(sim)).classes("text-white")
                if sim.abort_reason:
                    ui.label(ABORT_COPY.get(sim.abort_reason, "")).classes(
                        "text-negative"
                    )
                with ui.row().classes("gap-4 items-center"):
                    if sim.mode == "corrected":
                        ui.chip(
                            f"RTK {sim.rtk_status}", color=_RTK_COLOR[sim.rtk_status]
                        ).props("dense")
                        age = sim.correction_age_s
                        ui.label(
                            f"Correction age: {'—' if age is None else f'{age:.1f} s'}"
                        )
                        ui.label(f"Source: {sim.relay}").classes("text-grey-4")
                    ui.label(
                        f"{'Fixed time' if sim.mode == 'corrected' else 'Duration'}: "
                        f"{int(sim.observation_s)}/{sim.duration_s} s"
                    )
                    ui.label(
                        f"Accuracy: {sim.reported_acc_mm:.0f}/{sim.accuracy_mm} mm"
                    )
                    ui.label(f"Elapsed: {int(sim.wall_s)} s").classes("text-grey-5")
                if sim.outcome == "running" and sim.stall_s > 60:
                    ui.label(
                        f"No RTK Fixed for {int(sim.stall_s)} s. Aborts at 600 s."
                    ).classes("text-warning")
                if sim.relay_error:
                    ui.label(f"Correction source: {sim.relay_error}").classes(
                        "text-caption text-warning"
                    )

        progress()
        ui.timer(1.0, progress.refresh)
        prototype_controls(sim)


# ----------------------------------------------------------------------
# Variant B: Guided stepper with an event log
# ----------------------------------------------------------------------


def variant_b(sim: Sim) -> None:
    with ui.card().classes("w-full q-pa-md q-mt-md"):
        ui.label("Survey-In").classes("text-h6 text-white")
        with (
            ui.stepper()
            .props("vertical flat")
            .classes("w-full bg-transparent") as stepper
        ):
            with ui.step("Survey type"):
                with ui.row().classes("gap-4"):
                    for key, title, blurb in [
                        (
                            "plain",
                            "Plain Survey-In",
                            "Standalone averaging. Metre-level. Default 5 m / 120 s.",
                        ),
                        (
                            "corrected",
                            "Corrected Survey-In",
                            "Average RTK Fixed positions "
                            "against a known base. Centimetre-level. Default 50 mm / 300 s. "
                            "The base sends no RTCM while it runs.",
                        ),
                    ]:
                        with (
                            ui.card()
                            .classes("q-pa-md cursor-pointer w-72")
                            .on(
                                "click",
                                lambda k=key: (
                                    sim.set_mode(k),
                                    stepper.next()
                                    if k == "corrected"
                                    else stepper.set_value("Limits"),
                                ),
                            )
                        ):
                            ui.label(title).classes("text-subtitle1 text-white")
                            ui.label(blurb).classes("text-caption text-grey-4")

            with ui.step("Correction source"):

                @ui.refreshable
                def sources() -> None:
                    with ui.list().props("bordered separator").classes("w-full"):
                        for s in sim.sources:
                            with ui.item(
                                on_click=lambda n=s.name: (
                                    setattr(sim, "selected", n),
                                    setattr(sim, "verification", None),
                                    sources.refresh(),
                                )
                            ):
                                with ui.item_section().props("avatar"):
                                    ui.icon(
                                        "radio_button_checked"
                                        if s.name == sim.selected
                                        else "radio_button_unchecked"
                                    )
                                with ui.item_section():
                                    ui.item_label(s.name)
                                    ui.item_label(
                                        f"{s.caster}:{s.port}/{s.mountpoint} · v{s.version}"
                                    ).props("caption")
                    with ui.row().classes("q-mt-sm gap-2 items-center"):
                        verify_menu(sim, lambda: sources.refresh())
                        ui.button(
                            "New source",
                            icon="add",
                            on_click=lambda: source_editor(sim, None, sources.refresh),
                        ).props("flat")
                    if sim.verification:
                        with ui.column().classes("q-mt-sm gap-0"):
                            for stage, status, code in sim.verification.stages:
                                with ui.row().classes("items-center gap-2"):
                                    ui.icon(
                                        {
                                            "passed": "check_circle",
                                            "failed": "cancel",
                                            "warning": "warning",
                                            "skipped": "remove",
                                        }[status],
                                        color=_STATUS_COLOR[status],
                                    )
                                    ui.label(stage).classes("w-28")
                                    ui.label(code).classes("text-caption text-grey-5")

                sources()
                with ui.stepper_navigation():
                    ui.button("Next", on_click=stepper.next)
                    ui.button("Back", on_click=stepper.previous).props("flat")

            with ui.step("Limits"):
                with ui.row().classes("gap-4"):
                    ui.number("Min duration (s)").bind_value(sim, "duration_s")
                    ui.number("Accuracy limit (mm)").bind_value(sim, "accuracy_mm")
                ui.label().bind_text_from(
                    sim,
                    "mode",
                    lambda m: (
                        "Counts only time in RTK Fixed. Aborts after 10 min "
                        "without Fixed; capped at 1 h."
                        if m == "corrected"
                        else ""
                    ),
                ).classes("text-caption text-grey-5")
                with ui.stepper_navigation():

                    def _go() -> None:
                        err = sim.start()
                        if err:
                            ui.notify(err, type="warning")
                        else:
                            stepper.next()

                    ui.button("Start Survey-In", icon="play_arrow", on_click=_go).props(
                        "color=primary"
                    )
                    ui.button("Back", on_click=stepper.previous).props("flat")

            with ui.step("Run"):

                @ui.refreshable
                def run() -> None:
                    ui.label(outcome_text(sim)).classes("text-subtitle1 text-white")
                    if sim.abort_reason:
                        ui.label(ABORT_COPY.get(sim.abort_reason, "")).classes(
                            "text-negative"
                        )
                    with ui.grid(columns=2).classes("gap-x-6 gap-y-0"):
                        rows = [
                            (
                                "Observation time",
                                f"{int(sim.observation_s)} / {sim.duration_s} s",
                            ),
                            (
                                "Accuracy",
                                f"{sim.reported_acc_mm:.0f} / {sim.accuracy_mm} mm",
                            ),
                            ("Elapsed", f"{int(sim.wall_s)} s"),
                        ]
                        if sim.mode == "corrected":
                            age = sim.correction_age_s
                            rows += [
                                ("RTK", sim.rtk_status),
                                (
                                    "Correction age",
                                    "—" if age is None else f"{age:.1f} s",
                                ),
                                ("Correction source", sim.relay),
                            ]
                        for k, v in rows:
                            ui.label(k).classes("text-grey-5")
                            ui.label(v)
                    ui.label("Event log").classes("text-caption text-grey-5 q-mt-sm")
                    with ui.column().classes("gap-0 font-mono text-caption"):
                        for e in sim.events[-8:]:
                            ui.label(e)

                run()
                ui.timer(1.0, run.refresh)
                with ui.stepper_navigation():
                    ui.button("Cancel Survey", icon="stop", on_click=sim.cancel).props(
                        "color=negative outline"
                    ).bind_visibility_from(sim, "outcome", lambda o: o == "running")
                    ui.button(
                        "New survey", on_click=lambda: stepper.set_value("Survey type")
                    ).props("flat").bind_visibility_from(
                        sim, "outcome", lambda o: o != "running"
                    )
        prototype_controls(sim)


# ----------------------------------------------------------------------
# Variant C: Separate Correction Sources card + sticky status strip
# ----------------------------------------------------------------------


def variant_c(sim: Sim) -> None:
    @ui.refreshable
    def strip() -> None:
        if sim.outcome != "running" or sim.mode != "corrected":
            return
        color = {"fixed": "bg-positive", "float": "bg-warning"}.get(
            sim.rtk_status, "bg-negative"
        )
        age = sim.correction_age_s
        with (
            ui.row()
            .classes(f"w-full items-center gap-4 q-pa-sm text-black {color}")
            .style("position: sticky; top: 0; z-index: 10; border-radius: 4px")
        ):
            ui.icon("satellite_alt")
            ui.label(f"Corrected survey · {sim.selected}").classes("text-weight-bold")
            ui.label(f"RTK {sim.rtk_status}")
            ui.label(f"age {'—' if age is None else f'{age:.1f} s'}")
            ui.label(f"Fixed {int(sim.observation_s)}/{sim.duration_s} s")
            if sim.stall_s > 60:
                ui.label(f"abort in {int(STALL_ABORT_S - sim.stall_s)} s")
            ui.label("Base RTCM off-air").classes("q-ml-auto")

    strip()
    ui.timer(1.0, strip.refresh)

    with ui.card().classes("w-full q-pa-md q-mt-md"):
        ui.label("Correction Sources").classes("text-h6 text-white")
        ui.label(
            "Known bases whose corrections a Corrected survey-in feeds into the receiver."
        ).classes("text-caption text-grey-4")

        @ui.refreshable
        def table() -> None:
            with ui.grid(columns=5).classes("w-full items-center gap-x-4 q-mt-sm"):
                for h in ["Name", "Caster / mountpoint", "Verification", "", ""]:
                    ui.label(h).classes("text-caption text-grey-5")
                for s in sim.sources:
                    ui.label(s.name)
                    ui.label(f"{s.caster}:{s.port}/{s.mountpoint}").classes(
                        "text-grey-4"
                    )
                    with ui.element():
                        if s.name == sim.selected:
                            stage_chips(sim.verification)
                        else:
                            ui.label("—").classes("text-grey-6")

                    def _pick_and(n: str = s.name) -> None:
                        sim.selected = n
                        sim.verification = None
                        table.refresh()

                    with ui.row().classes("gap-0"):
                        ui.button(icon="ads_click", on_click=_pick_and).props(
                            "flat round dense"
                        ).tooltip("Select to verify")
                    ui.button(
                        icon="edit",
                        on_click=lambda src=s: source_editor(sim, src, _after_edit),
                    ).props("flat round dense")
            with ui.row().classes("gap-2 q-mt-sm"):
                verify_menu(sim, lambda: table.refresh())
                ui.button(
                    "Add source",
                    icon="add",
                    on_click=lambda: source_editor(sim, None, _after_edit),
                ).props("flat")

        def _after_edit() -> None:
            table.refresh()
            corr.options = {
                None: "None (plain Survey-In)",
                **{s.name: s.name for s in sim.sources},
            }
            corr.update()

        table()
        ui.timer(1.0, table.refresh)

    with ui.card().classes("w-full q-pa-md q-mt-md"):
        ui.label("Survey-In").classes("text-h6 text-white")
        ui.separator()

        def _corr_changed(e: object) -> None:
            val = getattr(e, "value", None)
            sim.set_mode("corrected" if val else "plain")
            if val:
                sim.selected = val

        corr = ui.select(
            {None: "None (plain Survey-In)", **{s.name: s.name for s in sim.sources}},
            value=None,
            label="Corrections from",
            on_change=_corr_changed,
        ).classes("w-96 q-mt-sm")
        with ui.row().classes("w-full gap-4"):
            ui.number("Min duration (s)").bind_value(sim, "duration_s").classes(
                "col-grow"
            )
            ui.number("Accuracy limit (mm)").bind_value(sim, "accuracy_mm").classes(
                "col-grow"
            )
        with ui.row().classes("gap-2 q-mt-sm"):

            def _start() -> None:
                err = sim.start()
                if err:
                    ui.notify(err, type="warning")

            ui.button("Start Survey-In", icon="play_arrow", on_click=_start).props(
                "color=primary"
            ).bind_enabled_from(sim, "outcome", lambda o: o != "running")
            ui.button("Cancel Survey", icon="stop", on_click=sim.cancel).props(
                "color=negative outline"
            ).bind_visibility_from(sim, "outcome", lambda o: o == "running")

        @ui.refreshable
        def bars() -> None:
            if sim.outcome == "idle":
                return
            ui.label(outcome_text(sim)).classes("text-white q-mt-sm")
            if sim.abort_reason:
                ui.label(ABORT_COPY.get(sim.abort_reason, "")).classes("text-negative")
            ui.label(
                f"{'RTK Fixed time' if sim.mode == 'corrected' else 'Duration'} "
                f"{int(sim.observation_s)} / {sim.duration_s} s"
            ).classes("text-caption")
            ui.linear_progress(
                min(1.0, sim.observation_s / max(1, sim.duration_s)), show_value=False
            )
            ui.label(
                f"Accuracy {sim.reported_acc_mm:.0f} mm (limit {sim.accuracy_mm})"
            ).classes("text-caption")
            ratio = sim.accuracy_mm / sim.reported_acc_mm if sim.reported_acc_mm else 0
            ui.linear_progress(min(1.0, ratio), show_value=False).props(
                "color=positive" if ratio >= 1 else "color=warning"
            )

        bars()
        ui.timer(1.0, bars.refresh)
        prototype_controls(sim)


# ----------------------------------------------------------------------
# Switcher + entry point
# ----------------------------------------------------------------------


def switcher(current: str) -> None:
    keys = list(VARIANTS)
    i = keys.index(current)

    def go(step: int) -> None:
        ui.navigate.to(f"/survey?variant={keys[(i + step) % len(keys)]}")

    with (
        ui.row()
        .classes("items-center gap-2 q-px-md q-py-xs bg-white text-black")
        .style(
            "position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);"
            "border-radius: 999px; box-shadow: 0 4px 16px rgba(0,0,0,.5); z-index: 9999"
        )
    ):
        ui.button(icon="chevron_left", on_click=lambda: go(-1)).props(
            "flat round dense color=black"
        )
        ui.label(f"PROTOTYPE {current} ({VARIANTS[current]})").classes(
            "text-weight-bold"
        )
        ui.button(icon="chevron_right", on_click=lambda: go(1)).props(
            "flat round dense color=black"
        )

    ui.keyboard(
        on_key=lambda e: (
            go(-1 if e.key.arrow_left else 1)
            if e.action.keydown and (e.key.arrow_left or e.key.arrow_right)
            else None
        )
    )


def render(variant: str) -> None:
    variant = variant.upper() if variant.upper() in VARIANTS else "A"
    sim = Sim()
    ui.timer(1.0, lambda: sim.tick(float(sim.speed)))
    {"A": variant_a, "B": variant_b, "C": variant_c}[variant](sim)
    switcher(variant)
