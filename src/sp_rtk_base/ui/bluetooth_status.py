"""Operator-facing copy for a Verification outcome.

This lives here rather than in ``ui/pages/input.py`` for the same reason
the Verification itself lives in the service layer: ``ui/pages/*`` is
excluded from the coverage gate, so the mapping from a result to the
sentence an operator actually reads would be untested by construction.
The page is left with rendering; the wording is decided here.

Two rules from ``CONTEXT.md`` are enforced by the tests rather than by
good intentions: a **Red** is always attributed to the Stage that
failed, never to whatever error text the layer below produced; and the
two **Warning** cases must not share wording, because a silent receiver
mid-survey and a receiver answering with something that is not RTCM are
very different evidence about the same configuration.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sp_rtk_base.models.bluetooth_models import (
    StageStatus,
    VerificationResult,
    VerificationStage,
    normalize_pin,
)
from sp_rtk_base.services.bluetooth_service import VerificationRefusedError

#: How the status line should be coloured.  These are NiceGUI's own
#: tone names, so the page can use them directly.
StatusTone = Literal["positive", "warning", "negative"]


@dataclass(frozen=True)
class StatusLine:
    """One line of status, and the tone to render it in."""

    text: str
    tone: StatusTone


#: Red copy, keyed by result code.  Each says which Stage failed *and*
#: what the operator's next move is — in particular whether the retry is
#: free, which is the difference between a device that is still paired
#: and one this application left Stranded.
_RED_COPY: dict[str, str] = {
    "device_not_found": (
        "Red — discover: the receiver was not found. Check that it is "
        "powered on, in range, and advertising."
    ),
    "pin_rejected": (
        "Red — pair: the PIN was rejected. The device is still paired, so "
        "correcting the PIN and testing again costs nothing."
    ),
    "pin_rejected_stranded": (
        "Red — pair: the PIN was rejected. The previous pairing was removed "
        "to test this PIN, so the device is now unpaired. Correct the PIN "
        "and test again to re-pair it."
    ),
    "bond_removal_failed": (
        "Red — pair: the existing pairing could not be removed, so the PIN "
        "was never tested. The device is still paired."
    ),
    "trust_failed": (
        "Red — trust: the device paired, but could not be marked trusted."
    ),
    "socket_refused": (
        "Red — connect: the receiver refused the RFCOMM connection. It is "
        "paired, but not accepting a data connection."
    ),
}

#: Green copy for the ``data`` Stage's two Warning outcomes.  Deliberately
#: unalike: silence is the benign mid-survey case, while non-RTCM bytes
#: are weaker evidence and may mean the wrong device answered.
_DATA_WARNING_COPY: dict[str, str] = {
    "no_data": (
        "Green — connected, but no RTCM yet. That is normal while the "
        "receiver is still surveying in. Save and Start will connect."
    ),
    "non_rtcm_data": (
        "Green — connected, but what came back was not RTCM. The receiver "
        "may still be emitting NMEA or a boot banner — or this may not be "
        "the receiver you meant. Save and Start will connect."
    ),
}

_GREEN_TEXT = (
    "Green — paired, connected, and receiving RTCM. Save and Start will "
    "connect, and will reconnect after the pairing is lost."
)

#: Refusal copy, keyed by the 409 code.  Three refusals share the status
#: with unrelated remedies, so each needs its own sentence rather than a
#: shared "conflict" message.  ``repair_confirmation_required`` is absent
#: on purpose: it is answered by a dialog, not by the status line.
_REFUSAL_COPY: dict[str, str] = {
    "relay_running": (
        "Stop the relay before testing — testing would interrupt the base "
        "station that rovers are using."
    ),
    "verification_in_progress": (
        "Another connection test is already running. Wait for it to finish, "
        "then try again."
    ),
}


def describe_result(result: VerificationResult) -> StatusLine:
    """Turn a Verification result into the line an operator reads.

    Args:
        result: The outcome of a Verification that ran.

    Returns:
        The status line and its tone.
    """
    if result.verdict == "red":
        stage = result.failing_stage
        failed = next(
            (s for s in result.stages if s.status is StageStatus.FAILED), None
        )
        code = failed.code if failed is not None else None
        text = _RED_COPY.get(code or "")
        if text is None:
            # An unrecognised code must still name the Stage: degrading
            # to a bare error string is the failure mode this whole
            # vocabulary exists to avoid.
            stage_name = stage.value if stage is not None else "unknown"
            detail = (failed.message if failed is not None else None) or "no detail"
            text = f"Red — {stage_name}: {detail}"
        return StatusLine(text=text, tone="negative")

    data = next((s for s in result.stages if s.stage is VerificationStage.DATA), None)
    if data is not None and data.status is StageStatus.WARNING:
        text = _DATA_WARNING_COPY.get(data.code or "")
        if text is None:
            text = f"Green — connected. {data.message or 'No RTCM seen.'}"
        return StatusLine(text=text, tone="warning")

    return StatusLine(text=_GREEN_TEXT, tone="positive")


def describe_refusal(exc: VerificationRefusedError) -> StatusLine:
    """Turn a refusal into the line an operator reads.

    Args:
        exc: The refusal raised by the service.

    Returns:
        The status line and its tone.  Falls back to the server's own
        message for a code this build does not know about, so a newly
        added refusal is never rendered as a blank.
    """
    return StatusLine(text=_REFUSAL_COPY.get(exc.code, exc.message), tone="warning")


def countdown_label(expires_at: datetime, now: datetime | None = None) -> str | None:
    """How much of the Green is left, as a short label.

    Args:
        expires_at: When the Green stops standing (absolute, UTC).
        now: Override for the current moment; defaults to now (UTC).

    Returns:
        A label like ``"12s"``, or ``None`` once the Green has expired.
        Rounds **up**, so a Green that is still valid never renders as
        ``"0s"`` — a countdown that reads zero while the button still
        works would be worse than none at all.
    """
    moment = now or datetime.now(timezone.utc)
    remaining = (expires_at - moment).total_seconds()
    if remaining <= 0:
        return None
    return f"{math.ceil(remaining)}s"


@dataclass(frozen=True)
class HeldGreen:
    """A Green the page is holding, and what it was taken against.

    A Green promises that Save and Start will connect *with these
    values*.  Editing the MAC or the PIN makes it a promise about
    something the operator is no longer looking at, so it is void — and
    that is expressed here as a predicate over the values currently in
    the form rather than as a flag some edit handler has to remember to
    clear.  A flag is only as good as the last person who added a field
    to the form; a predicate cannot be forgotten.

    Raises:
        ValueError: If constructed from a Red.  Only a Green entitles
            the operator to "Save & Start now →", so a Red must not be
            representable here at all.
    """

    result: VerificationResult
    mac_address: str
    pin: str

    def __post_init__(self) -> None:
        if self.result.verdict != "green":
            raise ValueError("Only a Green can be held")

    def is_valid_for(
        self, mac_address: str, pin: str, now: datetime | None = None
    ) -> bool:
        """Does this Green still describe the form's current values?

        Args:
            mac_address: The MAC currently in the form.
            pin: The PIN currently in the form, as typed.
            now: Override for the current moment; defaults to now (UTC).

        Returns:
            ``True`` while the Green is unexpired and still describes
            these values.
        """
        moment = now or datetime.now(timezone.utc)
        if moment >= self.result.expires_at:
            return False
        # Normalised, so that typing a space into the PIN field — or
        # clearing it when the relay default was what got proven — is
        # not mistaken for a change of PIN.
        return self.mac_address == mac_address and self.pin == normalize_pin(pin)
