"""Operator-facing copy for a Detection outcome.

This lives here rather than in ``ui/pages/survey.py`` and
``ui/pages/gps_config.py`` for the same reason the sweep itself lives in
the driver layer: ``ui/pages/*`` is excluded from the coverage gate, so
the mapping from a result to the sentence an operator reads would be
untested by construction — and it would be written twice, once per page.
The pages are left with rendering; the wording is decided here.

**The four outcomes must not share wording.** A port that ignores its
baud setting, a port nothing answered on, a port carrying traffic that
will not answer, and a port that could not be opened at all are four
different next actions. Collapsing any of them into "detection failed"
would recreate the exact dead end this feature exists to remove: a
message that tells an operator something is wrong without telling them
what to do about it.

**On vocabulary in the copy.** ``CONTEXT.md``'s ``_Avoid_`` lists govern
what *we* call things — in identifiers, logs, and design discussion — so
that one concept has one name. They are deliberately not applied to the
sentences an operator reads, exactly as ``bluetooth_status`` decided for
Bond and Stranded. Nobody holding a GPS receiver has heard of a
**Candidate** or a **Rate-indifferent** port. What the copy keeps is the
part that carries meaning: each outcome still says what was observed and
what to do next, and no two of them say it the same way.
"""

from __future__ import annotations

from sp_rtk_base.models.device_models import (
    CandidateVerdict,
    DetectionOutcome,
    DetectionResult,
)
from sp_rtk_base.services.device_service import DetectionRefusedError
from sp_rtk_base.services.drivers.base import BAUD_MISMATCH_HINT
from sp_rtk_base.ui.status_line import StatusLine

#: What Connect's "nothing answered" failure should say instead of
#: "check baud rate". Replacing rather than appending: the old advice
#: told the operator to go and think about baud rates, and the whole
#: point of map #140 decision 13 is to hand them an action instead.
_DETECT_INSTEAD = "click Detect to find the rate the receiver is listening at"

#: Said when a connect fails because the relay owns the port. Lives here
#: so both pages say it — only the Survey page used to, and the Advanced
#: GPS page reported the raw service error.
_RELAY_BUSY = (
    "Cannot connect while the relay is running. Go to the Dashboard and "
    "stop the relay first."
)

_RATE_INDIFFERENT = (
    "This port ignores the baud rate — the receiver answered at every rate "
    "tried, which is how a directly USB-connected receiver behaves. "
    "Whatever is wrong, it is not the baud rate."
)

_NOTHING_ANSWERED = (
    "No receiver answered at any baud rate. Check the cable, and that this "
    "is the right port."
)

#: Refusal copy, keyed by the 409 code. Two refusals share the status
#: with unrelated remedies, so each needs its own sentence rather than a
#: shared "conflict" message.
_REFUSAL_COPY: dict[str, str] = {
    "relay_running": (
        "Stop the relay before detecting. Detecting takes over the serial "
        "port, which would interrupt the base station that rovers are using."
    ),
    "device_connected": (
        "Already connected, so the baud rate is already known. Disconnect "
        "first if you want to check it again."
    ),
}


def describe_detection(result: DetectionResult) -> StatusLine:
    """Turn a Detection result into the line an operator reads.

    Args:
        result: The outcome of a Detection that ran.

    Returns:
        The status line and its tone.
    """
    if result.outcome is DetectionOutcome.RATE_INDIFFERENT:
        # Deliberately does not lead with the rate: on such a port the
        # reported rate is whatever was tried first, and presenting it
        # as a discovery would be dressing up a non-discovery.
        return StatusLine(text=_RATE_INDIFFERENT, tone="warning")

    if result.outcome is DetectionOutcome.FOUND:
        return StatusLine(text=_found_text(result), tone="positive")

    carrying = [
        c for c in result.candidates if c.verdict is CandidateVerdict.BYTES_NO_ANSWER
    ]
    if len(carrying) == 1:
        # Traffic at exactly one rate locates the link even though
        # nothing answered. At several rates it is noise, not a finding.
        rate = carrying[0].baud_rate
        return StatusLine(
            text=(
                f"No receiver answered, but there is traffic at {rate} baud. "
                "The link is probably at that rate and the receiver is not "
                "accepting commands on this port — check that UBX input is "
                "enabled for it."
            ),
            tone="warning",
        )

    return StatusLine(text=_NOTHING_ANSWERED, tone="negative")


def _found_text(result: DetectionResult) -> str:
    """Name the rate, and the receiver when it identified itself.

    Identity is free — an answer to the identity poll is what "found"
    means — but it is not guaranteed to be *specific*: a receiver whose
    model could not be resolved reports ``"Unknown"``, which is worse
    than saying nothing about it.
    """
    device = result.device
    model = device.model if device is not None else ""
    if not model or model == "Unknown":
        return f"Found a receiver at {result.baud_rate} baud."

    firmware = device.firmware_version if device is not None else ""
    identity = f"{model} (firmware {firmware})" if firmware else model
    return f"Found {identity} at {result.baud_rate} baud."


def detected_rate_to_apply(result: DetectionResult) -> int | None:
    """The rate the Baud Rate dropdown should be filled with, if any.

    Only a ``found`` result yields one. A Rate-indifferent port reports
    the first rate tried — every rate works there, so writing it back
    would present a non-discovery as a discovery, and the operator's
    existing selection already works. Lives here rather than in the page
    because "which results change the form" is a decision, not rendering.
    """
    if result.outcome is DetectionOutcome.FOUND:
        return result.baud_rate
    return None


def describe_detection_refusal(exc: DetectionRefusedError) -> StatusLine:
    """Turn a refusal into the line an operator reads.

    Args:
        exc: The refusal raised before anything was touched.

    Returns:
        The status line and its tone.
    """
    return StatusLine(
        text=_REFUSAL_COPY.get(exc.code, exc.message),
        tone="warning",
    )


def describe_port_failure(message: str) -> StatusLine:
    """Copy for a port that could not be opened at any rate.

    Not a Detection outcome and deliberately not worded like one:
    reporting a permission error as "no rate found" would send an
    operator hunting a baud rate that was never the problem.

    Takes the whole message rather than a port and a reason, because the
    sweep already composes one that names both — re-prefixing it here
    would say the port twice. All this layer adds is the remedy.
    """
    if "permission" in message.lower():
        # By far the most common cause on a Pi, and already documented
        # in docs/deployment-pi.md — worth saying rather than making
        # someone go and find it.
        message += (
            ". The service user probably needs to be in the dialout (or plugdev) group."
        )
    return StatusLine(text=message, tone="negative")


def describe_connect_failure(message: str) -> StatusLine:
    """Turn a failed Connect into the line an operator reads.

    Recognises exactly two failures and passes everything else through
    untouched — burying the layer below's error under a guess is worse
    than showing it.
    """
    if "relay is running" in message.lower():
        return StatusLine(text=_RELAY_BUSY, tone="warning")

    if BAUD_MISMATCH_HINT in message:
        return StatusLine(
            text=message.replace(BAUD_MISMATCH_HINT, _DETECT_INSTEAD),
            tone="negative",
        )

    # The driver already wraps most of its errors as "Connection
    # failed: ..."; anything that arrives unwrapped still needs to read
    # as a connect failure rather than as a bare sentence fragment.
    if not message.startswith("Connection failed:"):
        message = f"Connection failed: {message}"
    return StatusLine(text=message, tone="negative")
