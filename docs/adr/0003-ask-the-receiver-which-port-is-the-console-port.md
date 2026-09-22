# Ask the receiver which port is the console port, and fingerprint the answer on UBX counts

`apply_receiver_config` used to guard the application's own control link with
two contradictory guesses. The UBX-in liveness guard assumed the console was
always on USB. The post-baud-write reopen assumed it was always on UART1. The
docstring admitted the two had never been reconciled. On the reference rig the
second guess was true, so the guard protected a port the console wasn't using
and left the one it was using unguarded.

We now **ask the receiver**. Once per connect, the u-blox driver polls
`UBX-MON-COMMS`, writes a burst of well-formed UBX polls, and polls again. The
**console port** (see `CONTEXT.md`) is the one port that shows the probe's
fingerprint. Both the guard and the reopen use that single observed fact. When
the port can't be identified it is **unknown**, and both fall back to behaviour
that is safe whichever port we are on, rather than to either old guess.

## Why the rule is stricter than it looks like it needs to be

Every clause of the attribution rule is there because a simpler version failed
on paper or on the bench. Don't "simplify" it without re-running
`tools/probe_console_port.py` on real hardware.

- **Only Table 27 ports are candidates**, and the decode is `port << 8` with
  **UART2 = `0x0201`**, not `0x0200`. That's the Integration Manual
  (UBX-18010802 R16 §3.8), confirmed against u-blox's own `ubxlib`. The obvious
  table silently never matches UART2, and a miss looks exactly like "couldn't
  identify". gpsd has this bug today.
- **The test is "at least N", never "exactly N"**. The MON-COMMS polls are
  themselves received on the console port, and whether they land before or
  after the snapshot is undocumented.
- **Bytes alone are not enough; the UBX message count must also rise by at
  least the frames sent.** HPG 1.51 (protVer 27.50) reports two ports that
  Table 27 calls *Reserved*, `0x0101` and `0x0200`. Both carry heavy internal
  traffic (over 100 KB/s in one sample) and clear any byte threshold on their
  own. The bytes-only rule returned AMBIGUOUS on every bench run. The amended
  rule identified UART1 on 10 of 10 runs across two receivers.
- **Never read `txErrors.outputPort`**. pyubx2 decodes it whatever the protocol
  version. It is undefined at 27.31 and reads `2` on our 27.50 receivers, and it
  describes transmit errors, not our link.

## Considered options

- **Let the operator declare the port** (a profile field). Rejected: it asks a
  person a question the receiver can answer, and a wrong answer brings back the
  original bug looking authoritative.
- **Delete the guard** and rely on the post-Apply read-back. Rejected: once the
  link is cut there is nothing left to write the fix back with.
- **Reconcile on paper to UART1.** This was the pre-agreed fallback if the
  hardware had said no. It didn't, and UART1 would still be wrong for a
  USB-connected console.
- **Re-identify on every Apply.** Rejected: identification costs about 2 s, and
  the port can't change without breaking the serial link anyway.

## Consequences

- **Connect never fails because of identification.** An older receiver or a
  non-u-blox driver just gets an unknown console port with a reason
  (`no_answer`, `ambiguous`, `unrecognised`, `unsupported`). `unrecognised` is
  logged as our bug, not as the receiver's.
- **With an unknown console port, the guard protects UBX input on every port.**
  The shipped profile keeps UBX-in on both UARTs and leaves USB untouched, so
  this refuses only configs that turn UBX input off somewhere.
- **With an unknown console port, a UART baud write is followed by a liveness
  check.** If the link is dead, each changed UART's new rate is tried, and the
  one that answers becomes the console port.
- **A port answer outside `PortId` (I2C, SPI) counts as unknown.** A host serial
  device can't be attached there, so `PortId` and the Advanced GPS matrix it
  drives stay as they are.

Evidence: `docs/research/mon-comms-port-identification.md` on
`research/mon-comms`; bench results on issue #153; the full contract on
issue #154.
