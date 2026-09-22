"""One line of operator-facing status, and the tone to render it in.

Shared by the copy modules that decide what an operator reads —
``bluetooth_status`` for a Verification, ``detection_status`` for a
Detection. Those modules exist because ``ui/pages/*`` is excluded from
the coverage gate, so a result-to-sentence mapping left in a page would
be untested by construction; this type is the little bit of vocabulary
they have in common.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: How the status line should be coloured.  These are NiceGUI's own
#: tone names, so the page can use them directly.
StatusTone = Literal["positive", "warning", "negative"]


@dataclass(frozen=True)
class StatusLine:
    """One line of status, and the tone to render it in."""

    text: str
    tone: StatusTone
