"""The Bluetooth Verification — a dress rehearsal of the relay's connect path.

A *Verification* answers one question: would Save and Start connect?  It
runs against the values currently in the form rather than against what
is saved, and a Green means Save and Start will connect **and will
reconnect after the Bond is lost**.  That second half is the load-bearing
one — a Bond already in place carries the connection whatever the
configured PIN says (``pair_device`` fast-paths on ``Paired``), so only a
PIN exercised against a fresh Bond promises anything about the next
reboot or eviction.

The logic lives here rather than in ``ui/pages/input.py`` because
``ui/pages/*`` is excluded from the coverage gate: logic left in the page
is untested by construction.

See ``CONTEXT.md`` for the vocabulary, ``docs/adr/0001`` for the
force-repair safety policy and ``docs/adr/0002`` for the connect-Stage
mechanics and teardown ordering.
"""

from __future__ import annotations

import asyncio
import logging
import socket as socket_module
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sp_rtk_base.models.bluetooth_models import (
    StageResult,
    StageStatus,
    VerificationResult,
    VerificationStage,
    build_result,
    normalize_pin,
)

if TYPE_CHECKING:
    from sp_rtk_base_relay.core.input_sources.bluetooth_input import (
        BluetoothConfig,
    )

    from sp_rtk_base.services.config_service import ConfigService
    from sp_rtk_base.services.relay_service import RelayService

logger = logging.getLogger(__name__)

#: How long the ``data`` Stage listens for a first RTCM frame.  The
#: relay's own ``read_timeout`` is 1 s, which frequently misses a ~1 Hz
#: emitter; 3 s is long enough to see one without making a Red slow.
DATA_WINDOW_SECONDS = 3.0

#: Builds the ``BluetoothManager`` for an adapter, and the RFCOMM socket.
#: Injected so the service can be driven in tests without BlueZ,
#: dbus-fast, or an adapter.
ManagerFactory = Callable[[str], Any]
SocketFactory = Callable[[], Any]


class VerificationRefusedError(Exception):
    """The Verification did not run, and nothing was touched.

    A refusal is deliberately *not* a third verdict.  A verdict is the
    outcome of a probe that happened; folding a refusal in would force
    every consumer of ``verdict`` to handle a case where ``stages`` is
    meaningless (issue #127 §5).

    Three unrelated refusals share HTTP 409 with unrelated remedies
    (``relay_running``, ``verification_in_progress``,
    ``repair_confirmation_required``), so ``code`` — not the status — is
    what a client branches on.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: The Stages ``ensure_device_ready`` and ``force_repair`` each cover in
#: one opaque call.  Named once so the two paths that pass them cannot
#: drift apart.
_PRE_CONNECT_STAGES = (
    VerificationStage.DISCOVER,
    VerificationStage.PAIR,
    VerificationStage.TRUST,
)


def _mark_passed(
    recorded: dict[VerificationStage, StageResult],
    stages: tuple[VerificationStage, ...],
) -> None:
    """Record *stages* as passed.

    Args:
        recorded: The Stage results accumulated so far, mutated in place.
        stages: The Stages to mark.
    """
    for stage in stages:
        recorded[stage] = StageResult(stage=stage, status=StageStatus.PASSED)


def _default_manager_factory(adapter: str) -> Any:
    """Construct the relay's real ``BluetoothManager`` for *adapter*.

    Imported lazily so environments without dbus-fast (CI, macOS dev
    boxes) can still import this module.
    """
    from sp_rtk_base_relay.core.bluetooth_manager import BluetoothManager

    return BluetoothManager(adapter_name=adapter)


def _default_socket_factory() -> Any:
    """Create an ``AF_BLUETOOTH`` RFCOMM socket.

    The address family and protocol constants are imported from the
    relay's ``bluetooth_input`` rather than re-derived here, so the
    ``getattr(socket, "AF_BLUETOOTH", 31)`` fallback has exactly one
    definition and cannot drift away from the relay's.
    """
    from sp_rtk_base_relay.core.input_sources.bluetooth_input import (
        AF_BLUETOOTH,
        BTPROTO_RFCOMM,
    )

    return socket_module.socket(
        AF_BLUETOOTH,  # type: ignore[arg-type]
        socket_module.SOCK_STREAM,
        BTPROTO_RFCOMM,  # type: ignore[arg-type]
    )


class BluetoothVerificationService:
    """Runs Verifications, one at a time, against a real receiver."""

    def __init__(
        self,
        relay_service: RelayService,
        config_service: ConfigService,
        manager_factory: ManagerFactory = _default_manager_factory,
        socket_factory: SocketFactory = _default_socket_factory,
        data_window_seconds: float = DATA_WINDOW_SECONDS,
    ) -> None:
        """Wire up the service.

        Args:
            relay_service: Consulted for ``is_running`` before anything
                is touched.
            config_service: Supplies the durable Proven-PIN record.
            manager_factory: Builds the ``BluetoothManager``.
            socket_factory: Builds the RFCOMM socket.
            data_window_seconds: How long the ``data`` Stage listens.
                Shortened by tests, which have no real emitter to wait
                for and would otherwise pay the full window per case.
        """
        self._relay_service = relay_service
        self._config_service = config_service
        self._manager_factory = manager_factory
        self._socket_factory = socket_factory
        self._data_window_seconds = data_window_seconds

        # The process-scoped memo: what this server has proven itself.
        #
        # Keyed by (MAC, normalised PIN) rather than by browser session,
        # because two tabs proving the same PIN against the same
        # receiver have proven the same fact.  Only a force-repair the
        # server performed writes here — ``pair_device`` returns True
        # identically whether it exchanged a PIN or fast-pathed on an
        # existing Bond, so a plain ``ensure_device_ready`` pass can
        # never be credited as proof.
        #
        # It evaporates on restart, costing one extra demolition on the
        # first Verification afterwards.  Accepted: with no first-hand
        # knowledge, re-proving beats assuming.
        self._proven: set[tuple[str, str]] = set()

        # MACs this server left Stranded.  Held so that the consent
        # dialog does not warn about destroying a Bond that is already
        # gone — repair still *fires* there, it simply is not
        # destructive.
        self._stranded: set[str] = set()

        # Verifications are serialised process-wide.  Two live
        # ``BluetoothManager``s race for BlueZ's default agent with
        # per-instance ``_pending_pins``, so a concurrent Verification
        # can capture the other's ``RequestPinCode`` and reject it — a
        # false Red on a correct PIN.  Queuing was rejected: a waiter
        # can sit behind a 30 s scan and time out anyway.
        self._running = False

    # ------------------------------------------------------------------
    # The Proven-PIN predicate
    # ------------------------------------------------------------------

    def is_pin_proven(self, mac_address: str, pin: str) -> bool:
        """Has *pin* been exercised against a Bond with *mac_address*?

        Read only from server-held knowledge: the durable record on the
        saved profile, plus this process's memo.  Never from the
        request — a client that could assert proof could ask for *less*
        destruction and thereby buy a *stronger* promise, which is
        precisely the bug the Verification exists to kill.
        """
        normalised = normalize_pin(pin)
        if (mac_address, normalised) in self._proven:
            return True

        profile = self._config_service.get_input_config()
        if profile is None or profile.source != "bluetooth":
            return False
        stored_mac = profile.config.get("mac_address")
        return bool(
            profile.proven_pin
            and normalize_pin(profile.proven_pin) == normalised
            and stored_mac == mac_address
        )

    def believes_bond_exists(self, mac_address: str) -> bool:
        """Would a force-repair on *mac_address* destroy something?

        The app cannot see Bond state — there is no public ``Paired``
        accessor and ``find_device_by_mac`` is introspection-only — so
        this is a belief, not an observation.  The server assumes a Bond
        may exist unless it knows it stranded the device itself.  That
        conservative default is what makes the first Verification
        against a profile from the field prompt rather than silently
        demolish a working Bond.
        """
        return mac_address not in self._stranded

    def corroborates(self, mac_address: str, pin: str) -> bool:
        """Does the memo agree that *pin* is Proven for *mac_address*?

        Save calls this before writing a durable Proven-PIN record, so
        the one part of the design that outlives the process is not the
        one part still taking the client's word for it.
        """
        return (mac_address, normalize_pin(pin)) in self._proven

    # ------------------------------------------------------------------
    # The Verification itself
    # ------------------------------------------------------------------

    async def verify(
        self,
        mac_address: str,
        pin: str,
        adapter: str = "hci0",
        confirm_repair: bool = False,
    ) -> VerificationResult:
        """Run a Verification against *mac_address*.

        Args:
            mac_address: The receiver's MAC.  Required — the test path
                does no name discovery, because a second discovery
                semantic is a second thing to keep in step with the
                relay's.
            pin: The PIN as typed; normalised before use, by the same
                helper the profile uses, so the PIN proved here and the
                PIN the relay will present are provably one value.
            adapter: Bluetooth adapter name.
            confirm_repair: The operator has agreed to a destructive
                force-repair.

        Returns:
            The :class:`VerificationResult` — Green or Red.

        Raises:
            VerificationRefusedError: The Verification did not run and
                nothing was touched.
        """
        # Ordering is deliberate: relay-running is checked first, and
        # before a BluetoothManager exists.  Each manager opens its own
        # bus and ``RequestDefaultAgent`` is last-caller-wins, so a
        # manager built while the relay runs becomes BlueZ's default
        # agent — and the relay's next reconnect has its
        # ``RequestPinCode`` dispatched to *our* agent, which rejects
        # it.  A refusal issued after construction has already done the
        # damage it exists to prevent.
        if self._relay_service.is_running:
            raise VerificationRefusedError(
                code="relay_running",
                message=(
                    "The relay is running. Stop it before testing the "
                    "connection — testing would interrupt the base station."
                ),
            )

        if self._running:
            raise VerificationRefusedError(
                code="verification_in_progress",
                message=(
                    "Another connection test is already running. "
                    "Wait for it to finish, then try again."
                ),
            )

        normalised = normalize_pin(pin)
        repair_needed = not self.is_pin_proven(mac_address, normalised)

        # Repair *fires* whenever the PIN is unproven; it is
        # *destructive* only when a Bond is believed to exist.  Consent
        # is gated on the destruction, not on the firing — otherwise
        # the dialog would warn about demolishing a pairing that is
        # already gone.
        if (
            repair_needed
            and self.believes_bond_exists(mac_address)
            and not confirm_repair
        ):
            raise VerificationRefusedError(
                code="repair_confirmation_required",
                message=(
                    "This will remove the existing pairing and re-pair with "
                    "the PIN you entered. If the PIN is wrong, the device "
                    "will be left unpaired."
                ),
            )

        self._running = True
        manager: Any = None
        try:
            # The manager is built out here, not inside the Stage walk,
            # so the stale-handle release can reuse it.  Both calls are
            # pushed off the loop because BluetoothManager blocks on
            # D-Bus throughout.
            manager = await asyncio.to_thread(self._manager_factory, adapter)

            # Start from the same conditions a Start would.  Our own
            # manager is passed in rather than letting the helper build
            # one: a second manager on the bus is the default-agent
            # collision this service is otherwise careful to avoid.
            await release_stale_bluetooth_handle(mac_address, manager)

            return await asyncio.to_thread(
                self._run_verification,
                manager,
                mac_address,
                normalised,
                adapter,
                repair_needed,
            )
        finally:
            # The close lives here rather than alongside the rest of the
            # teardown so that *no* path can leak the manager — not a
            # factory that succeeded into a release that threw, not a
            # cancellation, not an unforeseen error mid-walk.  While our
            # manager lives it is BlueZ's default agent, and the relay's
            # next ``RequestPinCode`` would be dispatched to it and
            # rejected.  Ordering is preserved: the Stage walk's own
            # teardown has already done Disconnect and the socket by the
            # time this runs.
            if manager is not None:
                try:
                    await asyncio.to_thread(manager.close)
                except Exception as exc:
                    logger.warning("Error closing the BluetoothManager: %s", exc)
            self._running = False

    # ------------------------------------------------------------------
    # The Stage walk (synchronous: BluetoothManager blocks on D-Bus)
    # ------------------------------------------------------------------

    def _run_verification(
        self,
        manager: Any,
        mac_address: str,
        pin: str,
        adapter: str,
        repair_needed: bool,
    ) -> VerificationResult:
        """Walk the five Stages, then tear down exactly as the relay does."""
        from sp_rtk_base_relay.core.input_sources.bluetooth_input import BluetoothConfig

        from sp_rtk_base.models.config_models import DEFAULT_BT_SCAN_TIMEOUT_SECONDS

        # Timeouts are read off a real BluetoothConfig built from the
        # submitted values rather than hardcoded here.  That is what
        # keeps the Verification and the run waiting *identically*: if
        # the relay changes a default, this follows it, instead of
        # drifting until a Green stops predicting a Start.
        cfg: BluetoothConfig = BluetoothConfig(
            mac_address=mac_address,
            pin=pin,
            adapter_name=adapter,
            scan_timeout=DEFAULT_BT_SCAN_TIMEOUT_SECONDS,
        )

        recorded: dict[VerificationStage, StageResult] = {}
        sock: Any = None
        channel: int | None = None

        try:
            if repair_needed:
                reached_connect = self._walk_repair_path(
                    manager, mac_address, pin, cfg, recorded
                )
            else:
                reached_connect = self._walk_bundled_path(
                    manager, mac_address, pin, cfg, recorded
                )

            if reached_connect:
                channel = int(manager.discover_rfcomm_channel(mac_address))
                sock = self._open_socket(mac_address, channel, cfg, recorded)
                if sock is not None:
                    self._read_first_frame(sock, cfg, recorded)
        finally:
            # Teardown mirrors the relay's own ``disconnect()``:
            # BlueZ Disconnect, then our socket, then — in ``verify`` —
            # the manager.  There is deliberately no warm-ACL handoff to
            # a following Start: leaving BlueZ in a state a clean
            # shutdown would never produce would forfeit the claim that
            # Start begins from rehearsed conditions.
            self._teardown(manager, sock, mac_address)

        return build_result(recorded, rfcomm_channel=channel)

    def _walk_repair_path(
        self,
        manager: Any,
        mac_address: str,
        pin: str,
        cfg: BluetoothConfig,
        recorded: dict[VerificationStage, StageResult],
    ) -> bool:
        """Exercise *pin* against a fresh Bond, and attribute exactly.

        This route needs none of the bundled path's inference:
        ``force_repair`` already names the stage that failed in its
        error text, so a Stranding is *detected*, not guessed at.
        """
        from sp_rtk_base_relay.core.bluetooth_manager import BluetoothError

        try:
            manager.force_repair(mac_address, pin, scan_timeout=cfg.scan_timeout)
        except BluetoothError as exc:
            self._attribute_repair_failure(mac_address, pin, exc, recorded)
            return False

        # Only a force-repair the server performed mints proof.
        self._proven.add((mac_address, pin))
        self._stranded.discard(mac_address)
        _mark_passed(recorded, _PRE_CONNECT_STAGES)
        return True

    def _attribute_repair_failure(
        self,
        mac_address: str,
        pin: str,
        exc: Exception,
        recorded: dict[VerificationStage, StageResult],
    ) -> None:
        """Blame the right Stage for a ``force_repair`` failure."""
        text = str(exc)
        recorded[VerificationStage.DISCOVER] = StageResult(
            stage=VerificationStage.DISCOVER, status=StageStatus.PASSED
        )

        if "remove stage failed" in text:
            # The old Bond survived, so the device is no worse off and
            # the PIN was never exercised.
            recorded[VerificationStage.PAIR] = StageResult(
                stage=VerificationStage.PAIR,
                status=StageStatus.FAILED,
                code="bond_removal_failed",
                message=text,
            )
            return

        if "trust stage failed" in text:
            # Pairing succeeded, so the PIN *is* this device's real PIN
            # — record the proof.  Proof means the PIN is right, not
            # that everything downstream of it worked, and holding it
            # spares the operator a second demolition on the retry.
            self._proven.add((mac_address, pin))
            self._stranded.discard(mac_address)
            recorded[VerificationStage.PAIR] = StageResult(
                stage=VerificationStage.PAIR, status=StageStatus.PASSED
            )
            recorded[VerificationStage.TRUST] = StageResult(
                stage=VerificationStage.TRUST,
                status=StageStatus.FAILED,
                code="trust_failed",
                message=text,
            )
            return

        # The pair stage: the old Bond was removed and no new one could
        # be built.  The device is *Stranded* — damage this application
        # caused, not a neutral state it was found in — and the code
        # says so, because plain ``pin_rejected`` cannot distinguish
        # "still bonded, retry is free" from "now unbonded".
        self._stranded.add(mac_address)
        self._proven.discard((mac_address, pin))
        recorded[VerificationStage.PAIR] = StageResult(
            stage=VerificationStage.PAIR,
            status=StageStatus.FAILED,
            code="pin_rejected_stranded",
            message=text,
        )

    def _walk_bundled_path(
        self,
        manager: Any,
        mac_address: str,
        pin: str,
        cfg: BluetoothConfig,
        recorded: dict[VerificationStage, StageResult],
    ) -> bool:
        """Take the relay's own ``ensure_device_ready``, and infer on failure.

        ``ensure_device_ready`` is one opaque call spanning discover,
        pair and trust behind a single ``BluetoothError``, and the relay
        exposes no seam to drive them separately.  Success therefore
        needs no attribution at all; failure gets one cheap probe.
        """
        from sp_rtk_base_relay.core.bluetooth_manager import BluetoothError

        try:
            manager.ensure_device_ready(
                pin=pin,
                device_name=None,
                mac_address=mac_address,
                scan_timeout=cfg.scan_timeout,
            )
        except BluetoothError as exc:
            self._attribute_bundled_failure(manager, mac_address, exc, recorded)
            return False

        # This path pairs and trusts, so a Bond demonstrably exists now
        # — even if the device had been Stranded before.  Forgetting to
        # say so would leave the memo believing there is nothing left to
        # destroy, and the *next* unproven PIN would force-repair this
        # live Bond with no dialog: the silent demolition the consent
        # handshake exists to prevent.  It mints no *proof*, though:
        # ``pair_device`` fast-paths on an existing Bond, so a pass here
        # still says nothing about the PIN.
        self._stranded.discard(mac_address)
        _mark_passed(recorded, _PRE_CONNECT_STAGES)
        return True

    def _attribute_bundled_failure(
        self,
        manager: Any,
        mac_address: str,
        exc: Exception,
        recorded: dict[VerificationStage, StageResult],
    ) -> None:
        """Infer which Stage of the bundled call failed.

        One probe, not the two issue #127 §3 specified: its second probe
        existed to separate ``pair`` from ``channel``, and issue #129
        dropped the ``channel`` Stage on discovering that
        ``discover_rfcomm_channel`` is a stub ``return 1`` that cannot
        fail.  What remains is device-absent versus everything else.

        ``pair`` and ``trust`` stay indistinguishable here, and that is
        a deliberate trade: ``trust`` failing after ``pair`` succeeded
        is a D-Bus write failure and vanishingly rare, while blaming a
        pairing problem on the wrong Stage would invent a new class of
        misleading Red.
        """
        text = str(exc)
        try:
            found = bool(manager.find_device_by_mac(mac_address))
        except Exception:
            found = True

        if not found:
            recorded[VerificationStage.DISCOVER] = StageResult(
                stage=VerificationStage.DISCOVER,
                status=StageStatus.FAILED,
                code="device_not_found",
                message=text,
            )
            return

        recorded[VerificationStage.DISCOVER] = StageResult(
            stage=VerificationStage.DISCOVER, status=StageStatus.PASSED
        )
        recorded[VerificationStage.PAIR] = StageResult(
            stage=VerificationStage.PAIR,
            status=StageStatus.FAILED,
            code="pin_rejected",
            message=text,
        )

    def _open_socket(
        self,
        mac_address: str,
        channel: int,
        cfg: BluetoothConfig,
        recorded: dict[VerificationStage, StageResult],
    ) -> Any:
        """Open the RFCOMM socket — the ``connect`` Stage.

        The socket *is* the connection, not a prediction of one: the
        relay never D-Bus-``Connect()``s, because SPP devices reject it,
        so this is the same act the run performs rather than a cheaper
        stand-in for it.
        """
        sock = self._socket_factory()
        try:
            sock.settimeout(cfg.connect_timeout)
            sock.connect((mac_address, channel))
        except OSError as exc:
            recorded[VerificationStage.CONNECT] = StageResult(
                stage=VerificationStage.CONNECT,
                status=StageStatus.FAILED,
                code="socket_refused",
                message=str(exc),
            )
            try:
                sock.close()
            except Exception:
                logger.debug("Ignoring error closing a socket that never opened")
            return None

        # The channel is reported as a *detail on this Stage* rather
        # than as its own Stage or a form field: issue #129 dropped the
        # `channel` Stage because `discover_rfcomm_channel` is a stub
        # `return 1` that cannot fail, and #131 removed the form field
        # for the same reason. It is still worth saying which channel
        # the socket actually used.
        recorded[VerificationStage.CONNECT] = StageResult(
            stage=VerificationStage.CONNECT,
            status=StageStatus.PASSED,
            message=f"RFCOMM channel {channel}",
        )
        return sock

    def _read_first_frame(
        self,
        sock: Any,
        cfg: BluetoothConfig,
        recorded: dict[VerificationStage, StageResult],
    ) -> None:
        """Listen for a first RTCM frame — the ``data`` Stage.

        Warning-only, never fatal.  A receiver mid-survey may
        legitimately not be emitting yet, and failing on that would
        create a new class of false Red.

        Reads in ``read_timeout``-sized chunks until the window closes:
        the relay's 1 s read timeout frequently misses a ~1 Hz emitter.
        The buffer is resynced on the ``0xD3`` preamble before
        validating, because ``is_valid_rtcm_frame`` validates from byte
        0 and a mid-stream read starts mid-frame.
        """
        from sp_rtk_base_relay.rtcm_decoder import RTCMMessageDecoder

        sock.settimeout(cfg.read_timeout)
        deadline = time.monotonic() + self._data_window_seconds
        buffer = bytearray()

        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(8192)
            except TimeoutError:
                # The paced case: ``read_timeout`` elapsed with nothing
                # to read.  This is what spends the window.
                continue
            except OSError as exc:
                # The socket is gone; further reads cannot succeed, and
                # retrying would burn a core for the rest of the window
                # since a failing ``recv`` returns instantly.
                logger.debug("RFCOMM read ended early: %s", exc)
                break
            if not chunk:
                # A zero-length read means the peer closed.  Same
                # reasoning: nothing more is coming.
                break
            buffer.extend(chunk)
            if _contains_rtcm_frame(bytes(buffer), RTCMMessageDecoder):
                recorded[VerificationStage.DATA] = StageResult(
                    stage=VerificationStage.DATA, status=StageStatus.PASSED
                )
                return

        if buffer:
            # Two unlike cases share the Warning outcome and must not
            # share wording.  This is the weaker evidence of the two:
            # benign only while a receiver is still emitting NMEA or a
            # boot banner, and otherwise the wrong device answered on
            # this channel.
            recorded[VerificationStage.DATA] = StageResult(
                stage=VerificationStage.DATA,
                status=StageStatus.WARNING,
                code="non_rtcm_data",
                message=(
                    "The device sent data, but no valid RTCM frame. It may "
                    "still be emitting NMEA or a boot banner — or this may "
                    "not be the receiver you meant."
                ),
            )
            return

        recorded[VerificationStage.DATA] = StageResult(
            stage=VerificationStage.DATA,
            status=StageStatus.WARNING,
            code="no_data",
            message=(
                "Connected, but the receiver sent nothing yet. This is normal "
                "while it is still surveying in."
            ),
        )

    def _teardown(self, manager: Any, sock: Any, mac_address: str) -> None:
        """BlueZ Disconnect, then the socket.

        The ordering mirrors the relay's ``disconnect()`` so BlueZ owns
        the teardown of the channel state before we drop our local
        handle.  The socket-first order is the one abandoned in relay
        v2.1.2 that produced ``Address already in use`` on the next
        connect.  Each step is independently wrapped so no single
        failure short-circuits the rest.

        The manager's own ``close()`` is the third step and belongs to
        :meth:`verify`, which runs it on every path including the ones
        that never reach here.
        """
        if manager is not None:
            try:
                manager.disconnect_device(mac_address)
            except Exception as exc:
                logger.warning(
                    "Error disconnecting %s over D-Bus: %s", mac_address, exc
                )

        if sock is not None:
            try:
                sock.close()
            except Exception as exc:
                logger.warning("Error closing the RFCOMM socket: %s", exc)


def _contains_rtcm_frame(buffer: bytes, decoder: Any) -> bool:
    """Is there a valid RTCM 3 frame anywhere in *buffer*?

    Resyncs on the ``0xD3`` preamble before validating.  A bare
    ``is_valid_rtcm_frame`` on raw ``recv`` output fails even for a
    perfectly healthy stream, because it validates from byte 0 and a
    mid-stream read almost always starts mid-frame.  CRC-24Q makes a
    false positive on the resync offset nearly impossible.
    """
    start = 0
    while True:
        offset = buffer.find(b"\xd3", start)
        if offset == -1:
            return False
        if decoder.is_valid_rtcm_frame(buffer[offset:]):
            return True
        start = offset + 1


async def release_stale_bluetooth_handle(
    mac_address: str, manager: Any | None = None
) -> None:
    """Best-effort: ask BlueZ to drop a stale connection for *mac_address*.

    If a previous instance exited uncleanly (``SIGKILL``, OOM, power-loss
    during shutdown) BlueZ can still believe the receiver is connected —
    opening RFCOMM then fails with ``Address already in use`` or hangs
    waiting for an already-leased channel.

    This *only* asks BlueZ to drop the connection on its side; it does
    not un-pair, un-trust, or remove the device.  Errors are swallowed
    and logged, because neither a start nor a Verification should fail
    over a handle that may not have been stuck in the first place.

    Called from :meth:`RelayService.start_relay` — every path into the
    relay, not just auto-start — and from the Verification, so that the
    Verification is never *more forgiving than the Start it predicts*.

    Args:
        mac_address: The Bluetooth MAC of the configured receiver.
        manager: An existing ``BluetoothManager`` to reuse.  The
            Verification passes its own: building a second one would put
            two managers on the bus contending for BlueZ's default
            agent, which is the very collision the Verification is
            careful to avoid.  When omitted, one is built and closed
            here.
    """
    owned = manager is None
    mgr: Any = manager
    try:
        if mgr is None:
            try:
                # Imported lazily so environments without dbus-fast (CI,
                # macOS dev boxes) don't pay the import cost or fail at
                # module load.
                from sp_rtk_base_relay.core.bluetooth_manager import BluetoothManager
            except ImportError:
                logger.debug(
                    "BluetoothManager unavailable; skipping stale-handle "
                    "release for %s",
                    mac_address,
                )
                return
            mgr = BluetoothManager()

        # disconnect_device is sync-but-blocks-on-D-Bus; push it off-loop
        # so a wedged BlueZ can't stall a start.  A short budget is fine:
        # if BlueZ doesn't ack quickly, the handle wasn't really held.
        await asyncio.wait_for(
            asyncio.to_thread(mgr.disconnect_device, mac_address),
            timeout=5.0,
        )
        logger.info("Pre-disconnected stale Bluetooth handle for %s", mac_address)
    except TimeoutError:
        logger.warning(
            "Timed out releasing stale Bluetooth handle for %s; continuing anyway",
            mac_address,
        )
    except Exception as exc:
        # Most common cause: the device wasn't connected — entirely fine.
        logger.debug(
            "Stale-handle release for %s skipped (%s); continuing",
            mac_address,
            exc,
        )
    finally:
        if owned and mgr is not None:
            try:
                mgr.close()
            except Exception:
                logger.debug("BluetoothManager.close() raised; ignoring", exc_info=True)


def mac_from_input_config(config: dict[str, Any]) -> str | None:
    """Pull the receiver MAC out of a Bluetooth input config.

    ``address`` is the legacy key; profiles written by older versions
    still carry it.
    """
    mac = config.get("mac_address") or config.get("address")
    return mac if isinstance(mac, str) and mac else None
