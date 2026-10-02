"""The Verification vocabulary, shared by every kind of Verification.

A Verification rehearses a connect path against values the operator is about
to use, Stage by Stage, and ends Green or Red (see CONTEXT.md). The Bluetooth
input Verification and the Correction source Verification (issue #194) walk
different Stages of the one :class:`VerificationStage` vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel

#: How long a Green stands before it must be re-taken.  Re-founded by
#: issue #129 as a UX judgement — how long a typed PIN and MAC stay
#: trustworthy — rather than the BlueZ eviction measurement it was
#: originally derived from, which was taken against relay v2.1.2.
GREEN_TTL_SECONDS = 30.0


class VerificationStage(str, Enum):
    """One named, operator-meaningful step of a Verification.

    One vocabulary for every kind of Verification; each kind walks its own
    ordered subset (:data:`BLUETOOTH_STAGES`, :data:`CORRECTION_SOURCE_STAGES`).
    A step earns a name here only if it can actually *fail*.  That rule
    is why there is no ``channel`` stage: the relay's
    ``discover_rfcomm_channel`` is a stub ``return 1``, so a channel
    Stage would be structurally incapable of going red and would teach
    operators to stop reading Stages at all (issue #129, decision 2).
    The channel number is reported as a detail on :attr:`CONNECT`.
    """

    DISCOVER = "discover"
    PAIR = "pair"
    TRUST = "trust"
    CONNECT = "connect"
    CASTER = "caster"
    AUTH = "auth"
    MOUNTPOINT = "mountpoint"
    DATA = "data"

    @classmethod
    def ordered(cls) -> list[VerificationStage]:
        """Return the Bluetooth Verification's Stages, in the order it walks them.

        Callers render every Stage, including the ones a failure meant
        we never reached — "we never got as far as trying" is
        information (issue #127 §4).
        """
        return list(BLUETOOTH_STAGES)


class StageStatus(str, Enum):
    """The outcome of one Stage.

    :attr:`WARNING` exists so that a Stage can fall short without
    voiding a Green — failing the run on it would manufacture a Red for
    a configuration that works (issue #127 §4, charting decision 7).
    """

    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"


class StageResult(BaseModel):
    """What one Stage did.

    ``code`` is drawn from a small closed set that UI copy and tests key
    off; ``message`` carries the raw text from the layer below for the
    expandable detail and the log.  Asserting on a code is stable;
    asserting on ``"interface not found on this object: org.bluez.Device1"``
    is how a test becomes BlueZ-version-dependent (issue #127 §4).
    """

    stage: VerificationStage
    status: StageStatus
    code: str | None = None
    message: str | None = None


class VerificationResult(BaseModel):
    """The outcome of a Verification that ran.

    A refusal to run is **not** represented here: it is an HTTP 409, not
    a third verdict.  A verdict is the outcome of a probe that happened,
    and folding a refusal in would force every consumer to handle a case
    where ``stages`` means nothing (issue #127 §5).
    """

    verdict: Literal["green", "red"]
    failing_stage: VerificationStage | None = None
    stages: list[StageResult]
    rfcomm_channel: int | None = None
    verified_at: datetime
    expires_at: datetime


def build_result(
    recorded: Mapping[VerificationStage, StageResult],
    rfcomm_channel: int | None = None,
    verified_at: datetime | None = None,
    order: Sequence[VerificationStage] | None = None,
) -> VerificationResult:
    """Assemble a :class:`VerificationResult` from the Stages that ran.

    Two rules the Input page and the tests both depend on are applied
    here rather than at each call site:

    * Stages that were never reached are reported ``skipped``, not
      omitted — "we never got as far as trying" is information.
    * The verdict is Red iff some Stage ``failed``.  A ``warning`` keeps
      the Green, which is what makes the silent mid-survey receiver a
      benign outcome instead of a false Red.

    Args:
        recorded: The Stages that actually ran, keyed by Stage.
        rfcomm_channel: The channel ``connect`` used, when it got that far.
        verified_at: Override for the moment of the Verification; defaults
            to now (UTC).  Injected by tests so expiry is assertable.
        order: The Stages this kind of Verification walks, in order;
            defaults to the Bluetooth Verification's.

    Returns:
        The assembled result, with absolute UTC ``verified_at`` /
        ``expires_at`` — the client owns the visible countdown.
    """
    taken = verified_at or datetime.now(timezone.utc)
    stages = [
        recorded.get(stage, StageResult(stage=stage, status=StageStatus.SKIPPED))
        for stage in (order if order is not None else BLUETOOTH_STAGES)
    ]
    failing = next((s.stage for s in stages if s.status is StageStatus.FAILED), None)
    return VerificationResult(
        verdict="red" if failing is not None else "green",
        failing_stage=failing,
        stages=stages,
        rfcomm_channel=rfcomm_channel,
        verified_at=taken,
        expires_at=taken + timedelta(seconds=GREEN_TTL_SECONDS),
    )


#: The Bluetooth input Verification's Stages, in order.
BLUETOOTH_STAGES: tuple[VerificationStage, ...] = (
    VerificationStage.DISCOVER,
    VerificationStage.PAIR,
    VerificationStage.TRUST,
    VerificationStage.CONNECT,
    VerificationStage.DATA,
)

#: The Correction source Verification's Stages, in order (issue #194). They
#: map one-for-one to the Relay's typed NTRIP errors.
CORRECTION_SOURCE_STAGES: tuple[VerificationStage, ...] = (
    VerificationStage.CONNECT,
    VerificationStage.CASTER,
    VerificationStage.AUTH,
    VerificationStage.MOUNTPOINT,
    VerificationStage.DATA,
)
