"""Stage chips and verdict for a Correction source Verification (issue #194).

Shows each Stage (connect -> caster -> auth -> mountpoint -> data) as a chip,
the Green or Red verdict, and how long a Green still stands. A Green is void
once the values it was taken against change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from nicegui import ui

from sp_rtk_base.models.verification_models import (
    StageStatus,
    VerificationResult,
)
from sp_rtk_base.services.verification import VerificationRefusedError
from sp_rtk_base.ui.verification_status import countdown_label

_CHIP_COLOURS = {
    StageStatus.PASSED: "positive",
    StageStatus.FAILED: "negative",
    StageStatus.WARNING: "warning",
    StageStatus.SKIPPED: "grey-7",
}
_CHIP_ICONS = {
    StageStatus.PASSED: "check",
    StageStatus.FAILED: "close",
    StageStatus.WARNING: "warning",
    StageStatus.SKIPPED: "remove",
}

# What each failure code means to the operator.
_CODE_TEXT = {
    "dns": "the caster's name doesn't resolve",
    "refused": "the caster refused the connection",
    "timeout": "the caster didn't answer in time",
    "tls_handshake": "the TLS handshake failed (is it a TLS port?)",
    "tls_certificate": "the caster's TLS certificate isn't trusted",
    "other": "the connection failed",
    "bad_reply": "the reply isn't from an NTRIP caster",
    "rejected": "the caster rejected the username or password",
    "not_offered": "the caster doesn't offer this mountpoint now",
    "silent": "the caster sent no data",
    "not_rtcm3": "the data isn't RTCM 3",
    "no_reference_position": (
        "no reference station position (1005/1006) yet; the caster may send it rarely"
    ),
}


class VerificationPanel:
    """The chips, verdict and Green countdown, in the current container."""

    def __init__(self) -> None:
        self._result: VerificationResult | None = None
        with ui.column().classes("w-full gap-1") as self._root:
            self._chips = ui.row().classes("gap-1 items-center")
            self._verdict = ui.label("").classes("text-caption")
        self._root.set_visibility(False)
        self._timer = ui.timer(1.0, self._tick)

    async def run(
        self,
        verify: Callable[[], Awaitable[VerificationResult]],
        button: ui.button,
    ) -> None:
        """Run a Verification, showing it here; ``button`` is busy meanwhile."""
        self.running()
        button.disable()
        try:
            self.show(await verify())
        except VerificationRefusedError as exc:
            self.refused(exc.message)
        finally:
            button.enable()

    def reset(self) -> None:
        """Forget what was shown (e.g. another source was chosen)."""
        self._result = None
        self._chips.clear()
        self._verdict.text = ""
        self._root.set_visibility(False)

    def running(self) -> None:
        """A Verification is under way."""
        self._result = None
        self._root.set_visibility(True)
        self._chips.clear()
        self._verdict.text = "Verifying… (up to about 20 s)"
        self._verdict.classes(replace="text-caption text-grey-4")

    def show(self, result: VerificationResult) -> None:
        """Show a finished Verification."""
        self._result = result
        self._root.set_visibility(True)
        self._chips.clear()
        with self._chips:
            for stage in result.stages:
                ui.chip(
                    stage.stage.value,
                    icon=_CHIP_ICONS[stage.status],
                    color=_CHIP_COLOURS[stage.status],
                ).props(
                    f'dense text-color=white data-testid="stage-{stage.stage.value}" '
                    f'data-status="{stage.status.value}"'
                )
        self._tick()

    def refused(self, message: str) -> None:
        """The Verification didn't run."""
        self._result = None
        self._root.set_visibility(True)
        self._chips.clear()
        self._verdict.text = message
        self._verdict.classes(replace="text-caption text-warning")

    def void(self) -> None:
        """The values changed: a Green no longer stands."""
        if self._result is not None and self._result.verdict == "green":
            self._result = None
            self._chips.clear()
            self._verdict.text = (
                "The form changed since it was verified — Verify again."
            )
            self._verdict.classes(replace="text-caption text-grey-4")

    def _tick(self) -> None:
        result = self._result
        if result is None:
            return
        if result.verdict == "red":
            failing = next(s for s in result.stages if s.status is StageStatus.FAILED)
            reason = _CODE_TEXT.get(failing.code or "", failing.message or "failed")
            self._verdict.text = f"Red at {failing.stage.value}: {reason}."
            self._verdict.classes(replace="text-caption text-negative")
            return
        left = countdown_label(result.expires_at)
        if left is None:
            self._result = None
            self._verdict.text = "The Green has expired — Verify again."
            self._verdict.classes(replace="text-caption text-grey-4")
            return
        warnings = [s for s in result.stages if s.status is StageStatus.WARNING]
        note = ""
        if warnings:
            code = warnings[0].code or ""
            note = f" Warning: {_CODE_TEXT.get(code, warnings[0].message or code)}."
        self._verdict.text = (
            f"Green for {left}: a Corrected survey-in started now would "
            f"receive corrections.{note}"
        )
        self._verdict.classes(replace="text-caption text-positive")
