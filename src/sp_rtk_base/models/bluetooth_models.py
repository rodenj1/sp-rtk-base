"""Vocabulary for the Bluetooth Verification.

A *Verification* is a dress rehearsal of the relay's own connect path,
run against the values currently in the form rather than against what is
saved (see ``CONTEXT.md``).  This module holds the words it is described
in — the Stage names, the Stage outcomes, the result shape, and PIN
normalisation — so the service, the API, the Input page, and the tests
all key off one definition rather than four that agree by accident.

This module must **not** import :mod:`sp_rtk_base.models.config_models`:
``config_models`` imports :func:`normalize_pin` from here, and the
dependency has to run one way.  Putting the Stage enum in ``api_models``
instead would make the service layer import the API layer merely to log
a stage name, inverting the layering.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel

#: The PIN the relay falls back to when none is configured
#: (``BluetoothConfig.pin``).  A blank form field must normalise to this
#: value and not to ``""``, or the PIN a Verification proves is not the
#: PIN the relay will present.
DEFAULT_BT_PIN = "0000"

#: How long a Green stands before it must be re-taken.  Re-founded by
#: issue #129 as a UX judgement — how long a typed PIN and MAC stay
#: trustworthy — rather than the BlueZ eviction measurement it was
#: originally derived from, which was taken against relay v2.1.2.
GREEN_TTL_SECONDS = 30.0


def normalize_pin(pin: str | None) -> str:
    """Return the PIN the relay would actually use for *pin*.

    Applied inside :class:`~sp_rtk_base.models.config_models.InputProfile`
    so that every construction path — UI save, ``PUT /api/input``,
    profile import — normalises identically.  The goal is not that the
    call sites agree but that there is only one of them (issue #127 §9).

    Args:
        pin: The raw PIN as typed, or ``None``.

    Returns:
        The stripped PIN, or :data:`DEFAULT_BT_PIN` when it is blank.
    """
    if pin is None:
        return DEFAULT_BT_PIN
    return pin.strip() or DEFAULT_BT_PIN


class VerificationStage(str, Enum):
    """One named, operator-meaningful step of a Verification.

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
    DATA = "data"

    @classmethod
    def ordered(cls) -> list[VerificationStage]:
        """Return the Stages in the order the connect path walks them.

        Callers render every Stage, including the ones a failure meant
        we never reached — "we never got as far as trying" is
        information (issue #127 §4).
        """
        return [cls.DISCOVER, cls.PAIR, cls.TRUST, cls.CONNECT, cls.DATA]


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

    Returns:
        The assembled result, with absolute UTC ``verified_at`` /
        ``expires_at`` — the client owns the visible countdown.
    """
    taken = verified_at or datetime.now(timezone.utc)
    stages = [
        recorded.get(stage, StageResult(stage=stage, status=StageStatus.SKIPPED))
        for stage in VerificationStage.ordered()
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
