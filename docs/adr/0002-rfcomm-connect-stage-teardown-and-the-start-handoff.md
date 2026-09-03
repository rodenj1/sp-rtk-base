# The connect Stage leaves BlueZ exactly as a clean shutdown would, and Start shares its stale-handle release

The `connect` Stage of a Verification opens a real `AF_BLUETOOTH` / `SOCK_STREAM`
/ `BTPROTO_RFCOMM` socket, because in relay 3.0.0 that socket *is* the
connection: `ensure_device_ready` deliberately never calls `connect_device()`
("SPP devices reject D-Bus `Connect()` with NotAvailable… the RFCOMM socket
connection itself establishes the Bluetooth connection"). There is no cheaper
D-Bus connect to stand in for it, and no SDP step to pass first —
`discover_rfcomm_channel` is a stub that returns `1` unconditionally.

The Stage therefore tears down the way `BluetoothInputSource.disconnect()` does,
in the same order and for the same reason: `Device1.Disconnect` **first**, then
`socket.close()`, then `manager.close()`, each independently wrapped so no
failure short-circuits the rest. It does not attempt to hand a warm ACL link to
the Start that may follow seconds later. The Verification's whole claim is that
Start begins from the conditions it rehearsed; leaving BlueZ in a state a clean
relay shutdown would never produce breaks that claim in exchange for a few
seconds.

The handoff is instead made honest at the other end. The best-effort
stale-handle release moves out of the auto-start path and into
`RelayService.start_relay`, and the Verification calls the same helper.

## Considered options

**Driving `BluetoothInputSource.connect()` wholesale** is the strongest
anti-drift option — it cannot diverge from the path it predicts, because it *is*
that path. Rejected because it collapses five stages behind one
`InputSourceError`, worse than the four-way bundling ADR 0001 already works
around with post-hoc probes, and because force-repair is a `BluetoothManager`
method with no `InputSource` equivalent, so the manager is needed regardless.
Drift is contained structurally instead: timeouts are read off a real
`BluetoothConfig` built from the submitted values rather than hardcoded, and the
socket constants are imported from the relay rather than re-derived. The
duplicated surface is four socket calls.

**Leaving the stale-handle release where it was**, in the auto-start path only,
would have kept this ticket inside the Verification. Rejected: the release sits
inside `if not settings.auto_start: return`, so the four manual Start paths —
UI, API, handoff, and the "Save & Start now →" action this effort adds — never
get it. A Verification that pre-releases while Start does not is *more forgiving
than the thing it predicts*, which mints a Green for a Start that will fail. The
alternative repair, dropping the release from the Verification to match Start's
strictness, trades a false Green for a false Red in the same unclean-exit case
and tells the operator nothing about how to fix it.

**Mitigating BlueZ eviction between close and Start** — holding the socket open,
forcing a re-scan — was the shape this decision was expected to take. It is
answered by not being a problem: `_async_wait_for_device_interface` reaches the
populated `Device1` state via "~20-30 s of active discovery, OR a second scan,
OR **a successful RFCOMM connection**", so a Verification that reached `connect`
has just populated the interface rather than stripped it, and relay 3.0.0 polls
up to `scan_timeout=30` with active discovery in any case. The measurements that
motivated mitigation were taken on app v0.3.12 against relay **v2.1.2** — the
fixed-5 s-recovery-scan version that this poll loop was written to replace.

## Consequences

- **The 30 s Green expiry is no longer a BlueZ number.** It was adopted to match
  a measured eviction window; that measurement no longer describes shipped code.
  The duration is unchanged but its justification is now a UX one — how long a
  typed PIN and MAC stay trustworthy as a promise — and it should be argued and
  revised on those terms, not by re-measuring BlueZ.
- **The `channel` Stage is dropped**, leaving five: `discover`, `pair`, `trust`,
  `connect`, `data`. It could only ever pass. The channel number appears as a
  detail on `connect`, where it is actually used.
- **Auto-start loses its own pre-release call**, which becomes redundant. One
  release, one place, on every path that starts the relay.
- **Verifications are serialized process-wide**; a concurrent one is refused
  with 409 `verification_in_progress` rather than queued. Two live
  `BluetoothManager`s race for BlueZ's default agent and keep per-instance
  `_pending_pins`, so a second Verification can capture the first's
  `RequestPinCode` and reject it — a false Red on a correct PIN. Queuing was
  rejected because the waiter can sit behind a 30 s scan and time out anyway.
- **409 is never actionable on its own.** Three refusals now share the status
  with unrelated remedies — `repair_confirmation_required` (re-post with
  `confirm_repair`), `relay_running` (stop the relay), `verification_in_progress`
  (retry shortly). Clients branch on the code.
- **The manager is closed before the response is written**, not merely before
  the coroutine ends, so a following "Save & Start now →" can never build the
  relay's manager while the Verification's agent is still registered.
- **One eviction claim is still unmeasured** and is handed to hardware
  acceptance: Verification → close → wait 45 s → Start should succeed. If it
  does not, mitigation returns as a fresh ticket with real numbers.
