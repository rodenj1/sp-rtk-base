# Verification proves the PIN by destroying the Bond, and only the server may decide to

A Bond, once BlueZ holds one, carries the connection whatever the configured PIN
says: `pair_device` fast-paths on `Paired`, and RFCOMM needs only the Bond. So a
Verification that merely connects proves nothing about the PIN, and a base
station can run for months on a PIN that will fail the moment BlueZ evicts the
Bond — unattended, at 3am. We therefore define **Green** as *will connect and
will reconnect after the Bond is lost*, and accept the only means of proving the
second half: **force-repair**, which discards a working Bond so the configured
PIN can be exercised against a fresh one. A Verification may deliberately leave
the device worse than it found it.

Because the destruction is real, it is gated: force-repair fires **iff the
normalised PIN is not Proven for this MAC**, judged solely from server-held
first-hand knowledge — the durable record on `InputProfile` and a process-scoped
memo of pairings the server itself performed.

## Considered options

**Letting the client suppress the repair** (a `repair: "auto" | "never"` request
field) was the obvious way to stop repeated pre-Save tests from demolishing the
Bond each one had just built. It is unsound under the definition above:
suppressing the repair makes `ensure_device_ready` fast-path on the existing
Bond, every stage passes, and the endpoint returns a Green asserting durability
that was never tested. Asking for *less* destruction would buy a *stronger*
promise — the original bug, reachable through the API. Hence the server-side
memo: the server believes only proof it created.

**Grandfathering existing profiles** as carrying proof for their stored PIN
would spare every operator a demolition on first use after upgrade. Rejected:
the reported failure is a profile bonded out-of-band via `bluetoothctl` whose
stored PIN was never exercised, so grandfathering mints proof for precisely the
population known to contain the bug.

## Consequences

- **Consent is a refusal, not a flag.** A destructive repair returns HTTP 409
  `repair_confirmation_required` and touches nothing until the request is
  repeated with `confirm_repair: true`. The predicate stays in one place: the
  page cannot evaluate it and does not try.
- **The prompt gates on a believed Bond, not on whether repair will run.** After
  a Stranding the server knows the Bond is gone, so the retry is silent.
- **Proof does not survive an app restart.** The memo is in-process, so a
  restart costs one extra demolition. Preferred to assuming: after a restart the
  server has no first-hand knowledge.
- **Save corroborates rather than trusts.** A submitted Proven PIN the memo
  cannot confirm is dropped — the Save still succeeds, the PIN simply reads
  unproven and the next Verification re-proves it.
- **Only force-repair mints proof.** `pair_device` returns `True` identically
  whether it exchanged a PIN or fast-pathed, and there is no public `Paired`
  accessor, so a plain pass can never be credited. This costs nothing: an
  unproven PIN always takes the repair path, so the two never contend.
