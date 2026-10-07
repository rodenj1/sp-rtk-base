"""What the UI says when Start didn't start the Relay (rtk_development#50)."""

from __future__ import annotations

from sp_rtk_base.services.relay_service import RelayStartRefusedError
from sp_rtk_base.ui.start_status import needs_setup, start_failure_text


class TestStartFailureText:
    def test_no_input_says_where_to_set_one_up(self) -> None:
        refused = RelayStartRefusedError("no_input", "No input source configured.")
        assert needs_setup(refused)
        assert start_failure_text(refused) == (
            "No input source configured — go to Input first"
        )

    def test_no_enabled_output_says_where_to_add_one(self) -> None:
        refused = RelayStartRefusedError("no_destinations", "No enabled destinations.")
        assert needs_setup(refused)
        assert start_failure_text(refused) == (
            "No enabled destinations — add one in Outputs first"
        )

    def test_a_saved_input_port_that_is_not_a_number(self) -> None:
        refused = RelayStartRefusedError(
            "config_invalid",
            "input.config.port must be an integer between 1 and 65535"
            " | Key: input.config.port",
        )
        assert not needs_setup(refused)
        assert start_failure_text(refused) == (
            "Failed to start: TCP input port is not a number. "
            "Re-save the Input config (Input page) and try again."
        )

    def test_another_saved_config_that_cannot_run_says_why(self) -> None:
        refused = RelayStartRefusedError(
            "config_invalid", "Output 'rtk2go' uses NTRIP v2, which needs a username."
        )
        assert start_failure_text(refused) == (
            "Failed to start relay: Output 'rtk2go' uses NTRIP v2, "
            "which needs a username."
        )

    def test_other_refusals_say_their_message(self) -> None:
        refused = RelayStartRefusedError("console_connected", "Disconnect the console.")
        assert not needs_setup(refused)
        assert start_failure_text(refused) == "Disconnect the console."

    def test_a_failure_bringing_the_relay_up(self) -> None:
        assert not needs_setup(ConnectionRefusedError("refused"))
        assert start_failure_text(ConnectionRefusedError("refused")) == (
            "Failed to start relay: refused"
        )
