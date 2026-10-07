"""What the UI says when Start didn't start the Relay (rtk_development#50).

The Dashboard's Start and the Input page's Save and Start both start
from the saved config, so they say the same thing; and ``ui/pages/*`` is
excluded from the coverage gate, so the wording is decided here, in a
covered module, as ``console_link_status`` does for a Console link.
"""

from __future__ import annotations

from sp_rtk_base.services.relay_service import RelayStartRefusedError, StartRefusal

# Refusals the operator fixes by configuring something; shown as a warning
# that says where.
SETUP_HINTS: dict[StartRefusal, str] = {
    "no_input": "No input source configured — go to Input first",
    "no_destinations": "No enabled destinations — add one in Outputs first",
}


def needs_setup(exc: Exception) -> bool:
    """Whether ``exc`` is a refusal the operator fixes on another page."""
    return isinstance(exc, RelayStartRefusedError) and exc.code in SETUP_HINTS


def start_failure_text(exc: Exception) -> str:
    """What to tell the operator when Start didn't start the Relay.

    A saved config that can't run is put in words: the raw error names
    the internal config tree (e.g. "input.config.port must be an integer
    between 1 and 65535 | Key: input.config.port").
    """
    if isinstance(exc, RelayStartRefusedError):
        if exc.code in SETUP_HINTS:
            return SETUP_HINTS[exc.code]
        if exc.code != "config_invalid":
            return exc.message
    text = exc.message if isinstance(exc, RelayStartRefusedError) else str(exc)
    if "port must be an integer" in text or "input.config.port" in text:
        return (
            "Failed to start: TCP input port is not a number. "
            "Re-save the Input config (Input page) and try again."
        )
    if "input.config" in text or "destinations" in text:
        return (
            "Failed to start: configuration error.  Check the "
            "Input and Outputs pages for fields that need "
            "valid values, then re-save."
        )
    return f"Failed to start relay: {text}"
