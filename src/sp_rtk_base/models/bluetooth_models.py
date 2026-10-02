"""Vocabulary for the Bluetooth Verification.

A *Verification* is a dress rehearsal of the relay's own connect path,
run against the values currently in the form rather than against what is
saved (see ``CONTEXT.md``).  The words every Verification is described in
— the Stage names, the Stage outcomes, the result shape — live in
:mod:`sp_rtk_base.models.verification_models` and are re-exported here.
This module adds what is Bluetooth's own: the default PIN and PIN
normalisation.

This module must **not** import :mod:`sp_rtk_base.models.config_models`:
``config_models`` imports :func:`normalize_pin` from here, and the
dependency has to run one way.  Putting the Stage enum in ``api_models``
instead would make the service layer import the API layer merely to log
a stage name, inverting the layering.
"""

from __future__ import annotations

#: The PIN the relay falls back to when none is configured
#: (``BluetoothConfig.pin``).  A blank form field must normalise to this
#: value and not to ``""``, or the PIN a Verification proves is not the
#: PIN the relay will present.
DEFAULT_BT_PIN = "0000"


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


# The Verification vocabulary is shared by every kind of Verification
# (issue #194); it lives in verification_models and is re-exported here.
from sp_rtk_base.models.verification_models import (  # noqa: E402
    GREEN_TTL_SECONDS as GREEN_TTL_SECONDS,
)
from sp_rtk_base.models.verification_models import (  # noqa: E402
    StageResult as StageResult,
)
from sp_rtk_base.models.verification_models import (  # noqa: E402
    StageStatus as StageStatus,
)
from sp_rtk_base.models.verification_models import (  # noqa: E402
    VerificationResult as VerificationResult,
)
from sp_rtk_base.models.verification_models import (  # noqa: E402
    VerificationStage as VerificationStage,
)
from sp_rtk_base.models.verification_models import (  # noqa: E402
    build_result as build_result,
)
