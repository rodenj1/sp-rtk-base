"""Tests for what the Connect panel shows about a Console link (rtk_development#43).

``ui/pages/*`` is excluded from the coverage gate, so what the two
Connection cards (Survey and GPS config) say about the link lives in
``ui/console_link_status.py`` and is tested here. Both cards render the
same functions, which is what makes them behave the same.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.models.config_models import InputProfile
from sp_rtk_base.models.device_models import (
    BluetoothLink,
    ConnectStage,
    ConnectStageCode,
    ConnectStageResult,
    ConnectStageStatus,
    DeviceConnectionState,
    DeviceStatus,
    PortId,
    SerialLink,
)
from sp_rtk_base.ui.console_link_status import (
    SET_UP_BLUETOOTH_PROMPT,
    bluetooth_module_line,
    cancel_allowed,
    connected_line,
    kind_locked,
    stage_rows,
)

MODULE = BluetoothLink(device_name="RTK_BASE_TST", mac="98:D3:71:FE:FC:47")


def _bluetooth_profile() -> InputProfile:
    return InputProfile(
        source="bluetooth",
        config={
            "device_name": "RTK_BASE_TST",
            "mac_address": "98:D3:71:FE:FC:47",
            "pin": "1234",
        },
    )


def _stages(
    *statuses: tuple[ConnectStageStatus, ConnectStageCode | None],
) -> list[ConnectStageResult]:
    """Every Stage, the first ones at *statuses* and the rest pending."""
    padded = [*statuses] + [(ConnectStageStatus.PENDING, None)] * 3
    return [
        ConnectStageResult(stage=stage, status=status, code=code)
        for stage, (status, code) in zip(ConnectStage, padded, strict=False)
    ]


class TestConnectedLine:
    """The status line names the link it is connected over."""

    def test_bluetooth_names_the_module_and_the_console_port(self) -> None:
        status = DeviceStatus(
            state=DeviceConnectionState.CONNECTED,
            link=MODULE,
            console_port=PortId.UART2,
        )

        assert (
            connected_line(status)
            == "Connected: Bluetooth · RTK_BASE_TST · console port UART2"
        )

    def test_bluetooth_with_an_unknown_console_port_says_so(self) -> None:
        status = DeviceStatus(state=DeviceConnectionState.CONNECTED, link=MODULE)

        assert (
            connected_line(status)
            == "Connected: Bluetooth · RTK_BASE_TST · console port unknown"
        )

    def test_a_module_without_a_name_is_named_by_its_mac(self) -> None:
        status = DeviceStatus(
            state=DeviceConnectionState.CONNECTED,
            link=BluetoothLink(mac="98:D3:71:FE:FC:47"),
            console_port=PortId.UART2,
        )

        assert (
            connected_line(status)
            == "Connected: Bluetooth · 98:D3:71:FE:FC:47 · console port UART2"
        )

    def test_serial_names_the_port_and_rate(self) -> None:
        status = DeviceStatus(
            state=DeviceConnectionState.CONNECTED,
            link=SerialLink(port="/dev/ttyUSB1", baud_rate=57600),
        )

        assert connected_line(status) == "Connected: /dev/ttyUSB1 @ 57600"

    def test_connected_without_a_link_is_just_connected(self) -> None:
        status = DeviceStatus(state=DeviceConnectionState.CONNECTED)

        assert connected_line(status) == "Connected"


class TestBluetoothModuleLine:
    """The Bluetooth side shows the Input profile's module, or nothing to use."""

    def test_names_the_module_and_its_mac(self) -> None:
        line = bluetooth_module_line(_bluetooth_profile(), None)

        assert line is not None
        assert line.startswith("RTK_BASE_TST · 98:D3:71:FE:FC:47 · ")

    def test_pairing_is_unchecked_before_any_connect(self) -> None:
        line = bluetooth_module_line(_bluetooth_profile(), None)

        assert line is not None
        assert line.endswith("pairing not checked yet")

    def test_paired_at_the_last_connect_once_one_found_or_made_the_bond(
        self,
    ) -> None:
        skipped = _stages((ConnectStageStatus.SKIPPED, ConnectStageCode.BONDED))
        paired = _stages((ConnectStageStatus.PASSED, None))

        assert str(bluetooth_module_line(_bluetooth_profile(), skipped)).endswith(
            "· paired at the last connect"
        )
        assert str(bluetooth_module_line(_bluetooth_profile(), paired)).endswith(
            "· paired at the last connect"
        )

    def test_a_refused_pin_is_not_paired_at_the_last_connect(self) -> None:
        # The PIN is only tried when there is no Bond.
        refused = _stages((ConnectStageStatus.FAILED, ConnectStageCode.PIN_REJECTED))

        assert str(bluetooth_module_line(_bluetooth_profile(), refused)).endswith(
            "· not paired at the last connect"
        )

    @pytest.mark.parametrize(
        "code",
        [ConnectStageCode.DEVICE_NOT_FOUND, ConnectStageCode.BLUETOOTH_UNAVAILABLE],
    )
    def test_a_pair_failure_that_says_nothing_of_the_bond_is_unknown(
        self, code: ConnectStageCode
    ) -> None:
        failed = _stages((ConnectStageStatus.FAILED, code))

        line = str(bluetooth_module_line(_bluetooth_profile(), failed))

        assert line.endswith("· pairing unknown")
        assert "not paired" not in line

    def test_no_line_without_a_bluetooth_input_profile(self) -> None:
        tcp = InputProfile(source="tcp", config={"host": "h", "port": 1})

        assert bluetooth_module_line(tcp, None) is None
        assert bluetooth_module_line(None, None) is None

    def test_the_prompt_sends_the_operator_to_the_input_page(self) -> None:
        assert "Input page" in SET_UP_BLUETOOTH_PROMPT


class TestStageRows:
    """The Stage list during and after a Bluetooth connect."""

    def test_one_row_per_stage_in_order(self) -> None:
        rows = stage_rows(_stages())

        assert [r.label for r in rows] == ["Pair", "Connect", "Identify"]
        assert {r.status for r in rows} == {ConnectStageStatus.PENDING}

    def test_a_skipped_pair_says_already_paired(self) -> None:
        rows = stage_rows(
            _stages((ConnectStageStatus.SKIPPED, ConnectStageCode.BONDED))
        )

        assert rows[0].detail == "Already paired"

    def test_a_failure_shows_on_its_stage_with_that_stages_advice(self) -> None:
        stages = [
            ConnectStageResult(
                stage=ConnectStage.PAIR, status=ConnectStageStatus.SKIPPED
            ),
            ConnectStageResult(
                stage=ConnectStage.CONNECT,
                status=ConnectStageStatus.FAILED,
                code=ConnectStageCode.SOCKET_REFUSED,
                message="Connection refused",
                advice="Stop the Relay.",
            ),
            ConnectStageResult(stage=ConnectStage.IDENTIFY),
        ]

        rows = stage_rows(stages)

        assert rows[1].status is ConnectStageStatus.FAILED
        assert rows[1].detail == "Connection refused. Stop the Relay."
        assert rows[2].detail is None

    def test_a_passed_stage_shows_what_it_found(self) -> None:
        stages = [
            ConnectStageResult(
                stage=ConnectStage.IDENTIFY,
                status=ConnectStageStatus.PASSED,
                message="u-blox ZED-F9P; console port UART2",
            )
        ]

        assert stage_rows(stages)[0].detail == "u-blox ZED-F9P; console port UART2"

    def test_no_rows_without_a_bluetooth_connect(self) -> None:
        assert stage_rows(None) == []


class TestControls:
    """When the toggle and Cancel can be used."""

    def test_the_kind_is_locked_while_connecting_or_connected(self) -> None:
        assert kind_locked(DeviceConnectionState.CONNECTING)
        assert kind_locked(DeviceConnectionState.CONNECTED)
        assert not kind_locked(DeviceConnectionState.DISCONNECTED)
        assert not kind_locked(DeviceConnectionState.ERROR)

    def test_cancel_is_disabled_while_the_connect_stage_runs(self) -> None:
        connecting = _stages(
            (ConnectStageStatus.SKIPPED, ConnectStageCode.BONDED),
            (ConnectStageStatus.RUNNING, None),
        )

        assert not cancel_allowed(connecting)

    def test_cancel_is_allowed_otherwise(self) -> None:
        pairing = _stages((ConnectStageStatus.RUNNING, None))

        assert cancel_allowed(pairing)
        assert cancel_allowed(None)
