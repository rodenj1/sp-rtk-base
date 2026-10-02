"""What every kind of Verification shares in the service layer."""

from __future__ import annotations


class VerificationRefusedError(Exception):
    """The Verification did not run, and nothing was touched.

    A refusal is deliberately *not* a third verdict.  A verdict is the
    outcome of a probe that happened; folding a refusal in would force
    every consumer of ``verdict`` to handle a case where ``stages`` is
    meaningless (issue #127 §5).

    Unrelated refusals share HTTP 409 with unrelated remedies
    (``relay_running``, ``verification_in_progress``,
    ``repair_confirmation_required``), so ``code`` — not the status — is
    what a client branches on.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
