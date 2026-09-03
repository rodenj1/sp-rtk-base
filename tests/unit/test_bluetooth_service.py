"""Tests for the Bluetooth Verification service.

A *Verification* is a dress rehearsal of the relay's own connect path.
These tests drive the real service with a fake `BluetoothManager` and a
fake socket, so they run without BlueZ, without dbus-fast, and without
an adapter.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from sp_rtk_base.models.bluetooth_models import (
    StageStatus,
    VerificationStage,
)
from sp_rtk_base.services.bluetooth_service import (
    BluetoothVerificationService,
    VerificationRefusedError,
)


class FakeRelayService:
    """Stands in for RelayService; only `is_running` is consulted."""

    def __init__(self, running: bool = False) -> None:
        self.is_running = running


class FakeManager:
    """A stand-in BluetoothManager recording what the service asked of it."""

    def __init__(
        self,
        ready_error: Exception | None = None,
        repair_error: Exception | None = None,
        device_present: bool = True,
    ) -> None:
        self.calls: list[str] = []
        self.closed = False
        self.ready_error = ready_error
        self.repair_error = repair_error
        self.device_present = device_present

    def ensure_device_ready(
        self, pin: str, device_name: str | None = None, **kwargs: Any
    ) -> tuple[str, int]:
        self.calls.append("ensure_device_ready")
        if self.ready_error is not None:
            raise self.ready_error
        return kwargs.get("mac_address") or "", 1

    def force_repair(self, mac_address: str, pin: str, **kwargs: Any) -> bool:
        self.calls.append("force_repair")
        if self.repair_error is not None:
            raise self.repair_error
        return True

    def find_device_by_mac(self, mac_address: str) -> bool:
        self.calls.append("find_device_by_mac")
        return self.device_present

    def discover_rfcomm_channel(self, mac_address: str) -> int:
        self.calls.append("discover_rfcomm_channel")
        return 1

    def disconnect_device(self, mac_address: str) -> bool:
        self.calls.append("disconnect_device")
        return True

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True


def _rtcm_frame() -> bytes:
    """A byte string containing a real, CRC-valid RTCM 3 frame.

    Built with the relay's own encoder rather than hand-rolled, so the
    expected value comes from an independent source of truth than the
    resync logic under test.  Prefixed with noise so the frame does NOT
    start at byte 0 — a mid-stream `recv` is the normal case, and
    `is_valid_rtcm_frame` validates from byte 0 only.
    """
    from sp_rtk_base_relay.rtcm_decoder import RTCMMessageDecoder

    payload = bytes([0x3E, 0xD0]) + bytes(20)
    header = bytes([0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF])
    body = header + payload
    crc = RTCMMessageDecoder.calc_crc24q(body)
    frame = body + crc.to_bytes(3, "big")
    assert RTCMMessageDecoder.is_valid_rtcm_frame(frame)
    return b"\xa5\x5a" + frame


class FakeSocket:
    """A stand-in RFCOMM socket."""

    def __init__(self, reads: list[Any] | None = None) -> None:
        self.timeouts: list[float] = []
        self.connected_to: tuple[str, int] | None = None
        self.closed = False
        self._reads = list(reads or [])

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def connect(self, address: tuple[str, int]) -> None:
        self.connected_to = address

    def recv(self, size: int) -> bytes:
        if not self._reads:
            raise TimeoutError("no data")
        nxt = self._reads.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def close(self) -> None:
        self.closed = True


class FakeConfigService:
    """Supplies the durable Proven-PIN record the predicate reads."""

    def __init__(self, input_profile: Any = None) -> None:
        self._input = input_profile

    def get_input_config(self) -> Any:
        return self._input


def build_service(
    relay_running: bool = False,
    manager: FakeManager | None = None,
    sock: FakeSocket | None = None,
    input_profile: Any = None,
    data_window_seconds: float = 0.01,
) -> tuple[BluetoothVerificationService, FakeManager, FakeSocket]:
    """Assemble a service over fakes, returning the fakes for assertions."""
    mgr = manager or FakeManager()
    skt = sock if sock is not None else FakeSocket([_rtcm_frame()])
    service = BluetoothVerificationService(
        relay_service=FakeRelayService(running=relay_running),  # type: ignore[arg-type]
        config_service=FakeConfigService(input_profile),  # type: ignore[arg-type]
        manager_factory=lambda adapter: mgr,
        socket_factory=lambda: skt,
        data_window_seconds=data_window_seconds,
    )
    return service, mgr, skt


class TestGreenPathWithAProvenPin:
    """A PIN already Proven for this MAC takes the non-destructive route."""

    @pytest.mark.asyncio
    async def test_all_five_stages_pass(self) -> None:
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
            verified_pin="1234",
        )
        service, mgr, skt = build_service(input_profile=profile)

        result = await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        assert result.verdict == "green"
        assert [s.status for s in result.stages] == [StageStatus.PASSED] * 5

    @pytest.mark.asyncio
    async def test_force_repair_is_not_fired_for_a_proven_pin(self) -> None:
        """Repairing on every run would be needlessly destructive."""
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
            verified_pin="1234",
        )
        service, mgr, _ = build_service(input_profile=profile)

        await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        assert "force_repair" not in mgr.calls
        assert "ensure_device_ready" in mgr.calls

    @pytest.mark.asyncio
    async def test_the_socket_is_opened_on_the_reported_channel(self) -> None:
        """The socket *is* the connection, not a prediction of one.

        The relay never D-Bus-connects — SPP devices reject `Connect()`
        — so there is no cheaper connect to mirror (issue #129).
        """
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
            verified_pin="1234",
        )
        service, _, skt = build_service(input_profile=profile)

        result = await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        assert skt.connected_to == ("AA:BB:CC:DD:EE:FF", 1)
        assert result.rfcomm_channel == 1


class TestRefusesWhileRelayIsRunning:
    """Charting decision 8, and the ordering that makes it matter.

    Silently stopping a live base station that rovers depend on is a
    worse surprise than a blocked button.  The check has to happen
    *before* a `BluetoothManager` is constructed: constructing one
    mid-run is exactly what steals BlueZ's default agent from the relay,
    so a refusal issued after construction has already done the damage
    it was meant to prevent.
    """

    @pytest.mark.asyncio
    async def test_refuses_with_the_relay_running_code(self) -> None:
        service, _, _ = build_service(relay_running=True)
        with pytest.raises(VerificationRefusedError) as exc:
            await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")
        assert exc.value.code == "relay_running"

    @pytest.mark.asyncio
    async def test_no_bluetooth_manager_is_constructed(self) -> None:
        """The damage this refusal exists to prevent is construction itself."""
        built: list[FakeManager] = []

        def factory(adapter: str) -> FakeManager:
            mgr = FakeManager()
            built.append(mgr)
            return mgr

        service = BluetoothVerificationService(
            relay_service=FakeRelayService(running=True),  # type: ignore[arg-type]
            config_service=FakeConfigService(),  # type: ignore[arg-type]
            manager_factory=factory,
            socket_factory=FakeSocket,
        )
        with pytest.raises(VerificationRefusedError):
            await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")
        assert built == []


class TestForceRepairTriggerPredicate:
    """Force-repair fires iff the normalised PIN is not Proven for this MAC.

    Not on every run (needlessly destructive), and not never (the bug).
    Proof is read only from server-held sources — the durable profile
    record and this process's memo — never from the request.
    """

    @pytest.mark.asyncio
    async def test_an_unproven_pin_takes_the_repair_path(self) -> None:
        service, mgr, _ = build_service()
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        assert "force_repair" in mgr.calls
        assert "ensure_device_ready" not in mgr.calls

    @pytest.mark.asyncio
    async def test_a_successful_repair_mints_proof(self) -> None:
        service, _, _ = build_service()
        assert not service.is_pin_proven("AA:BB:CC:DD:EE:FF", "1234")

        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )

        assert service.is_pin_proven("AA:BB:CC:DD:EE:FF", "1234")

    @pytest.mark.asyncio
    async def test_the_second_run_no_longer_repairs(self) -> None:
        """The memo is what stops a second test demolishing the first's Bond."""
        service, mgr, _ = build_service()
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        mgr.calls.clear()

        await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        assert "force_repair" not in mgr.calls
        assert "ensure_device_ready" in mgr.calls

    @pytest.mark.asyncio
    async def test_proof_is_scoped_to_the_device_it_was_proven_against(self) -> None:
        service, _, _ = build_service()
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        assert not service.is_pin_proven("11:22:33:44:55:66", "1234")

    @pytest.mark.asyncio
    async def test_a_plain_pass_never_mints_proof(self) -> None:
        """`pair_device` returns True identically whether it exchanged a PIN.

        So `ensure_device_ready` succeeding says nothing about the PIN,
        and crediting it would re-create the bug: a wrong PIN riding a
        pre-existing Bond to a Green.
        """
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
            verified_pin="1234",
        )
        service, mgr, _ = build_service(input_profile=profile)
        await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        assert "ensure_device_ready" in mgr.calls
        assert not service.corroborates("AA:BB:CC:DD:EE:FF", "1234")

    def test_a_blank_pin_is_proven_as_the_relay_default(self) -> None:
        """Proof is keyed by the normalised PIN, not the typed one."""
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "0000"},
            verified_pin="0000",
        )
        service, _, _ = build_service(input_profile=profile)
        assert service.is_pin_proven("AA:BB:CC:DD:EE:FF", "")

    def test_a_proven_pin_for_a_different_mac_does_not_carry_over(self) -> None:
        """Otherwise a MAC change inherits another device's proof."""
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "11:22:33:44:55:66", "pin": "1234"},
            verified_pin="1234",
        )
        service, _, _ = build_service(input_profile=profile)
        assert not service.is_pin_proven("AA:BB:CC:DD:EE:FF", "1234")


class TestConsentHandshake:
    """Automatic destructive re-pair is not acceptable.

    The operator typed a PIN so they expect *something* to change — but
    not that a working Bond is dropped to test it.  Only the server can
    evaluate the predicate, so consent is a refusal on the same
    endpoint: 409, touching nothing, re-posted with `confirm_repair`.
    """

    @pytest.mark.asyncio
    async def test_an_unproven_pin_is_refused_until_confirmed(self) -> None:
        service, mgr, _ = build_service()
        with pytest.raises(VerificationRefusedError) as exc:
            await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")
        assert exc.value.code == "repair_confirmation_required"

    @pytest.mark.asyncio
    async def test_the_refusal_touches_nothing(self) -> None:
        """Not even a BluetoothManager — the refusal precedes all of it."""
        built: list[FakeManager] = []
        service = BluetoothVerificationService(
            relay_service=FakeRelayService(running=False),  # type: ignore[arg-type]
            config_service=FakeConfigService(),  # type: ignore[arg-type]
            manager_factory=lambda adapter: built.append(FakeManager()) or built[-1],
            socket_factory=FakeSocket,
        )
        with pytest.raises(VerificationRefusedError):
            await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")
        assert built == []

    @pytest.mark.asyncio
    async def test_a_proven_pin_needs_no_confirmation(self) -> None:
        """Nothing destructive is about to happen, so nothing is asked."""
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
            verified_pin="1234",
        )
        service, _, _ = build_service(input_profile=profile)
        result = await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")
        assert result.verdict == "green"

    @pytest.mark.asyncio
    async def test_the_relay_running_refusal_is_checked_first(self) -> None:
        """Both are 409s; the ordering decides which remedy is offered."""
        service, _, _ = build_service(relay_running=True)
        with pytest.raises(VerificationRefusedError) as exc:
            await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")
        assert exc.value.code == "relay_running"


def _bt_error(text: str) -> Exception:
    from sp_rtk_base_relay.core.bluetooth_manager import BluetoothError

    return BluetoothError(text)


class TestStranded:
    """A device left with no Bond by a Verification that removed the old one.

    Named for damage the application caused, not a neutral state a
    device may innocently be in.  `pin_rejected` alone cannot tell
    "still bonded, retry is free" from "now unbonded".
    """

    @pytest.mark.asyncio
    async def test_a_rejected_pin_after_removal_is_reported_stranded(self) -> None:
        mgr = FakeManager(
            repair_error=_bt_error("force_repair: pair stage failed: PIN rejected")
        )
        service, _, _ = build_service(manager=mgr)

        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="9999", confirm_repair=True
        )

        assert result.verdict == "red"
        assert result.failing_stage == VerificationStage.PAIR
        pair = next(s for s in result.stages if s.stage == VerificationStage.PAIR)
        assert pair.code == "pin_rejected_stranded"

    @pytest.mark.asyncio
    async def test_a_stranding_mints_no_proof(self) -> None:
        mgr = FakeManager(
            repair_error=_bt_error("force_repair: pair stage failed: PIN rejected")
        )
        service, _, _ = build_service(manager=mgr)
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="9999", confirm_repair=True
        )
        assert not service.is_pin_proven("AA:BB:CC:DD:EE:FF", "9999")

    @pytest.mark.asyncio
    async def test_the_corrected_retry_prompts_for_nothing(self) -> None:
        """Recovery is a corrected retry and nothing more.

        With no Bond left there is nothing to destroy, so warning about
        destroying a pairing would describe something that is already
        gone.
        """
        mgr = FakeManager(
            repair_error=_bt_error("force_repair: pair stage failed: PIN rejected")
        )
        service, _, _ = build_service(manager=mgr)
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="9999", confirm_repair=True
        )

        mgr.repair_error = None
        result = await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        assert result.verdict == "green"

    @pytest.mark.asyncio
    async def test_a_failed_removal_leaves_the_device_bonded(self) -> None:
        """The old Bond survived, so this is not a Stranding."""
        mgr = FakeManager(
            repair_error=_bt_error("force_repair: remove stage failed: busy")
        )
        service, _, _ = build_service(manager=mgr)

        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )

        pair = next(s for s in result.stages if s.stage == VerificationStage.PAIR)
        assert pair.code == "bond_removal_failed"
        assert service.believes_bond_exists("AA:BB:CC:DD:EE:FF")

    @pytest.mark.asyncio
    async def test_a_trust_failure_still_proves_the_pin(self) -> None:
        """Pairing succeeded, so the PIN *is* this device's real PIN.

        Holding that proof spares the operator a second demolition on
        the retry — proof means the PIN is right, not that everything
        downstream of it worked.
        """
        mgr = FakeManager(
            repair_error=_bt_error("force_repair: trust stage failed: d-bus write")
        )
        service, _, _ = build_service(manager=mgr)

        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )

        assert result.failing_stage == VerificationStage.TRUST
        assert service.is_pin_proven("AA:BB:CC:DD:EE:FF", "1234")

    @pytest.mark.asyncio
    async def test_proof_of_a_different_pin_survives_a_stranding(self) -> None:
        """Reverting to a Proven PIN skips the repair and simply pairs."""
        service, mgr, _ = build_service()
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )

        mgr.repair_error = _bt_error("force_repair: pair stage failed: PIN rejected")
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="9999", confirm_repair=True
        )

        assert service.is_pin_proven("AA:BB:CC:DD:EE:FF", "1234")


class TestBundledPathAttribution:
    """`ensure_device_ready` bundles discover+pair+trust behind one error.

    The contract calls this out as the weak link, so it is covered
    explicitly rather than incidentally.
    """

    @pytest.mark.asyncio
    async def test_an_absent_device_is_blamed_on_discover(self) -> None:
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
            verified_pin="1234",
        )
        mgr = FakeManager(
            ready_error=_bt_error("Device not found"), device_present=False
        )
        service, _, _ = build_service(manager=mgr, input_profile=profile)

        result = await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        assert result.failing_stage == VerificationStage.DISCOVER
        discover = next(
            s for s in result.stages if s.stage == VerificationStage.DISCOVER
        )
        assert discover.code == "device_not_found"

    @pytest.mark.asyncio
    async def test_a_present_device_is_blamed_on_pair(self) -> None:
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
            verified_pin="1234",
        )
        mgr = FakeManager(ready_error=_bt_error("Failed to pair"), device_present=True)
        service, _, _ = build_service(manager=mgr, input_profile=profile)

        result = await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        assert result.failing_stage == VerificationStage.PAIR
        pair = next(s for s in result.stages if s.stage == VerificationStage.PAIR)
        assert pair.code == "pin_rejected"

    @pytest.mark.asyncio
    async def test_the_raw_bluez_text_is_kept_for_the_detail_view(self) -> None:
        """Codes are what tests and UI copy assert on; text is for humans."""
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
            verified_pin="1234",
        )
        mgr = FakeManager(
            ready_error=_bt_error("interface not found on this object"),
            device_present=True,
        )
        service, _, _ = build_service(manager=mgr, input_profile=profile)

        result = await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        pair = next(s for s in result.stages if s.stage == VerificationStage.PAIR)
        assert pair.message is not None
        assert "interface not found" in pair.message

    @pytest.mark.asyncio
    async def test_stages_after_the_failure_are_skipped(self) -> None:
        from sp_rtk_base.models.config_models import InputProfile

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
            verified_pin="1234",
        )
        mgr = FakeManager(
            ready_error=_bt_error("Device not found"), device_present=False
        )
        service, _, skt = build_service(manager=mgr, input_profile=profile)

        result = await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        assert [s.status for s in result.stages[1:]] == [StageStatus.SKIPPED] * 4
        assert skt.connected_to is None


class TestConnectStage:
    """Opening the RFCOMM socket. Required for a Green."""

    @pytest.mark.asyncio
    async def test_a_refused_socket_is_red_on_connect(self) -> None:
        class RefusingSocket(FakeSocket):
            def connect(self, address: tuple[str, int]) -> None:
                raise OSError(111, "Connection refused")

        service, _, _ = build_service(sock=RefusingSocket())
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )

        assert result.verdict == "red"
        assert result.failing_stage == VerificationStage.CONNECT
        connect = next(s for s in result.stages if s.stage == VerificationStage.CONNECT)
        assert connect.code == "socket_refused"

    @pytest.mark.asyncio
    async def test_the_connect_timeout_comes_from_the_relays_config(self) -> None:
        """Not a hardcoded 10.0 here that could drift from the relay's.

        Structural containment: if the relay changes the default, the
        Verification follows it instead of quietly ceasing to predict
        the Start.
        """
        from sp_rtk_base_relay.core.input_sources.bluetooth_input import BluetoothConfig

        service, _, skt = build_service()
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )

        defaults = BluetoothConfig()
        assert skt.timeouts[0] == defaults.connect_timeout
        assert skt.timeouts[1] == defaults.read_timeout


class TestTeardown:
    """Teardown mirrors the relay's own `disconnect()` exactly."""

    @pytest.mark.asyncio
    async def test_bluez_disconnect_precedes_the_socket_close(self) -> None:
        """The socket-first order is the one that produced `Address already
        in use` on the next connect, and was abandoned in relay v2.1.2."""
        service, mgr, skt = build_service()
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )

        assert mgr.calls.index("disconnect_device") < mgr.calls.index("close")
        assert skt.closed

    @pytest.mark.asyncio
    async def test_the_manager_is_closed_on_the_green_path(self) -> None:
        service, mgr, _ = build_service()
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        assert mgr.closed

    @pytest.mark.asyncio
    async def test_the_manager_is_closed_on_a_failure_path(self) -> None:
        """The narrow default-agent guarantee this map takes on.

        While our manager lives it is BlueZ's default agent, so a
        manager leaked by a failure path would have the relay's next
        `RequestPinCode` dispatched to it — and rejected.
        """
        mgr = FakeManager(
            repair_error=_bt_error("force_repair: pair stage failed: PIN rejected")
        )
        service, _, _ = build_service(manager=mgr)
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="9999", confirm_repair=True
        )
        assert mgr.closed

    @pytest.mark.asyncio
    async def test_the_manager_is_closed_even_when_a_stage_raises_wildly(self) -> None:
        """An unexpected exception must not leak BlueZ's default agent."""

        class ExplodingManager(FakeManager):
            def discover_rfcomm_channel(self, mac_address: str) -> int:
                raise RuntimeError("something entirely unforeseen")

        mgr = ExplodingManager()
        service, _, _ = build_service(manager=mgr)
        with pytest.raises(RuntimeError):
            await service.verify(
                mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
            )
        assert mgr.closed

    @pytest.mark.asyncio
    async def test_a_failing_disconnect_does_not_prevent_the_close(self) -> None:
        """Each teardown step is independently wrapped."""

        class GrumpyManager(FakeManager):
            def disconnect_device(self, mac_address: str) -> bool:
                self.calls.append("disconnect_device")
                raise RuntimeError("BlueZ said no")

        mgr = GrumpyManager()
        service, _, _ = build_service(manager=mgr)
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        assert mgr.closed


class TestDataStage:
    """Warning-only, never fatal — three outcomes, two of them warnings."""

    @pytest.mark.asyncio
    async def test_a_valid_frame_mid_stream_passes(self) -> None:
        """A real `recv` starts mid-frame, so the buffer must resync on 0xD3."""
        service, _, _ = build_service(sock=FakeSocket([_rtcm_frame()]))
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        data = next(s for s in result.stages if s.stage == VerificationStage.DATA)
        assert data.status == StageStatus.PASSED

    @pytest.mark.asyncio
    async def test_a_frame_split_across_two_reads_still_passes(self) -> None:
        frame = _rtcm_frame()
        service, _, _ = build_service(sock=FakeSocket([frame[:6], frame[6:]]))
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        data = next(s for s in result.stages if s.stage == VerificationStage.DATA)
        assert data.status == StageStatus.PASSED

    @pytest.mark.asyncio
    async def test_silence_is_a_warning_and_keeps_the_green(self) -> None:
        """The motivating case: a receiver still surveying in."""
        service, _, _ = build_service(sock=FakeSocket([]))
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        assert result.verdict == "green"
        data = next(s for s in result.stages if s.stage == VerificationStage.DATA)
        assert data.status == StageStatus.WARNING
        assert data.code == "no_data"

    @pytest.mark.asyncio
    async def test_non_rtcm_bytes_are_a_distinctly_worded_warning(self) -> None:
        """Weaker evidence than silence — possibly the wrong device."""
        service, _, _ = build_service(sock=FakeSocket([b"$GPGGA,junk\r\n"]))
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        assert result.verdict == "green"
        data = next(s for s in result.stages if s.stage == VerificationStage.DATA)
        assert data.status == StageStatus.WARNING
        assert data.code == "non_rtcm_data"

    @pytest.mark.asyncio
    async def test_the_two_warnings_do_not_share_wording(self) -> None:
        """CONTEXT.md requires the weaker case to say so in as many words."""
        silent, _, _ = build_service(sock=FakeSocket([]))
        noisy, _, _ = build_service(sock=FakeSocket([b"$GPGGA,junk\r\n"]))

        a = await silent.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        b = await noisy.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )

        msg_a = next(s for s in a.stages if s.stage == VerificationStage.DATA).message
        msg_b = next(s for s in b.stages if s.stage == VerificationStage.DATA).message
        assert msg_a != msg_b

    @pytest.mark.asyncio
    async def test_a_corrupt_preamble_does_not_pass(self) -> None:
        """CRC-24Q is what makes a resync false positive nearly impossible."""
        frame = bytearray(_rtcm_frame())
        frame[-1] ^= 0xFF  # break the CRC
        service, _, _ = build_service(sock=FakeSocket([bytes(frame)]))
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        data = next(s for s in result.stages if s.stage == VerificationStage.DATA)
        assert data.code == "non_rtcm_data"


class TestVerificationsAreSerialized:
    """Two live `BluetoothManager`s race for BlueZ's default agent.

    `_pending_pins` is per-instance, so a concurrent Verification can
    capture the other's `RequestPinCode` and reject it — a false Red on
    a correct PIN.  Queuing was rejected: a waiter can sit behind a 30 s
    scan and time out anyway, so the second caller is refused outright.
    """

    @pytest.mark.asyncio
    async def test_a_concurrent_verification_is_refused(self) -> None:
        import asyncio

        release = threading.Event()

        class SlowManager(FakeManager):
            def force_repair(self, mac_address: str, pin: str, **kwargs: Any) -> bool:
                self.calls.append("force_repair")
                release.wait(timeout=5)
                return True

        service, _, _ = build_service(manager=SlowManager())

        first = asyncio.create_task(
            service.verify(
                mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
            )
        )
        await asyncio.sleep(0.05)  # let the first reach the worker thread

        try:
            with pytest.raises(VerificationRefusedError) as exc:
                await service.verify(
                    mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
                )
            assert exc.value.code == "verification_in_progress"
        finally:
            release.set()
            await first

    @pytest.mark.asyncio
    async def test_the_slot_is_released_after_a_failure(self) -> None:
        """A wedged Verification must not block the machine forever."""

        class ExplodingManager(FakeManager):
            def discover_rfcomm_channel(self, mac_address: str) -> int:
                raise RuntimeError("boom")

        service, _, _ = build_service(manager=ExplodingManager())
        with pytest.raises(RuntimeError):
            await service.verify(
                mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
            )

        # The next call gets as far as the consent check, not the
        # in-progress refusal — proving the slot was freed.
        with pytest.raises(VerificationRefusedError) as exc:
            await service.verify(mac_address="11:22:33:44:55:66", pin="1234")
        assert exc.value.code == "repair_confirmation_required"


class TestTheVerificationReleasesTheStaleHandleToo:
    """The Verification must not be more forgiving than the Start it predicts.

    A stale BlueZ handle would fail the Start but, without this, not the
    Verification — a Green for a Start that then fails.  Dropping the
    release from the Verification instead would just trade a false Green
    for a false Red in the same case.
    """

    @pytest.mark.asyncio
    async def test_the_handle_is_released_before_the_stage_walk(self) -> None:
        service, mgr, _ = build_service()
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        assert mgr.calls.index("disconnect_device") < mgr.calls.index("force_repair")

    @pytest.mark.asyncio
    async def test_it_reuses_our_manager_rather_than_building_a_second(self) -> None:
        """Two managers on the bus is the default-agent collision itself."""
        built: list[FakeManager] = []

        def factory(adapter: str) -> FakeManager:
            mgr = FakeManager()
            built.append(mgr)
            return mgr

        service = BluetoothVerificationService(
            relay_service=FakeRelayService(running=False),  # type: ignore[arg-type]
            config_service=FakeConfigService(),  # type: ignore[arg-type]
            manager_factory=factory,
            socket_factory=lambda: FakeSocket([_rtcm_frame()]),
            data_window_seconds=0.01,
        )
        await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )

        assert len(built) == 1

    @pytest.mark.asyncio
    async def test_the_manager_is_closed_when_the_walk_never_runs(self) -> None:
        """A factory that succeeded into a release that threw must not leak."""
        mgr = FakeManager()

        async def _boom(mac: str, manager: Any = None) -> None:
            raise RuntimeError("BlueZ is wedged")

        service, _, _ = build_service(manager=mgr)
        import sp_rtk_base.services.bluetooth_service as _svc_mod

        original = _svc_mod.release_stale_bluetooth_handle
        _svc_mod.release_stale_bluetooth_handle = _boom  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError):
                await service.verify(
                    mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
                )
        finally:
            _svc_mod.release_stale_bluetooth_handle = original  # type: ignore[assignment]

        assert mgr.closed
        assert "force_repair" not in mgr.calls


class TestDegradedAttribution:
    """What the service does when its own attribution probe fails."""

    @pytest.mark.asyncio
    async def test_a_failing_probe_blames_pair_not_discover(self) -> None:
        """An unknown answer must not be reported as a confident one.

        Blaming `discover` would tell the operator the receiver is out of
        range, sending them to check power and distance when the real
        fault may be the PIN.  `pair` is the honest default: it is the
        Stage the bundled call most often dies in.
        """
        from sp_rtk_base.models.config_models import InputProfile

        class BlindManager(FakeManager):
            def find_device_by_mac(self, mac_address: str) -> bool:
                raise RuntimeError("D-Bus introspection failed")

        profile = InputProfile(
            source="bluetooth",
            config={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
            verified_pin="1234",
        )
        mgr = BlindManager(ready_error=_bt_error("something went wrong"))
        service, _, _ = build_service(manager=mgr, input_profile=profile)

        result = await service.verify(mac_address="AA:BB:CC:DD:EE:FF", pin="1234")

        assert result.failing_stage == VerificationStage.PAIR

    @pytest.mark.asyncio
    async def test_a_manager_that_will_not_close_still_returns_a_result(self) -> None:
        """The operator gets their answer even when cleanup misbehaves."""

        class StubbornManager(FakeManager):
            def close(self) -> None:
                self.calls.append("close")
                raise RuntimeError("the event loop will not stop")

        service, _, _ = build_service(manager=StubbornManager())
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        assert result.verdict == "green"

    @pytest.mark.asyncio
    async def test_a_socket_that_will_not_close_does_not_break_the_result(self) -> None:
        class StubbornSocket(FakeSocket):
            def close(self) -> None:
                raise RuntimeError("fd is wedged")

        service, _, _ = build_service(sock=StubbornSocket([_rtcm_frame()]))
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        assert result.verdict == "green"

    @pytest.mark.asyncio
    async def test_an_empty_read_is_not_mistaken_for_data(self) -> None:
        """A zero-length recv is a closed peer, not bytes on the wire."""
        service, _, _ = build_service(sock=FakeSocket([b"", b""]))
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        data = next(s for s in result.stages if s.stage == VerificationStage.DATA)
        assert data.code == "no_data"


class TestTheChannelIsReportedAsAConnectDetail:
    """#129 dropped the `channel` Stage and #131 removed the form field.

    Both for the same reason: `discover_rfcomm_channel` is a stub
    `return 1`, so there is nothing to choose and nothing that can fail.
    Which channel the socket actually used is still worth saying.
    """

    @pytest.mark.asyncio
    async def test_the_connect_stage_names_the_channel(self) -> None:
        service, _, _ = build_service()
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        connect = next(s for s in result.stages if s.stage == VerificationStage.CONNECT)
        assert connect.message == "RFCOMM channel 1"

    @pytest.mark.asyncio
    async def test_the_result_still_carries_the_channel_for_api_consumers(self) -> None:
        service, _, _ = build_service()
        result = await service.verify(
            mac_address="AA:BB:CC:DD:EE:FF", pin="1234", confirm_repair=True
        )
        assert result.rfcomm_channel == 1
