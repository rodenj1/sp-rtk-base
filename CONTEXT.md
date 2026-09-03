# sp-rtk-base

The web UI and REST API an operator uses to configure and run an RTK base
station. The actual RTCM relaying is done by the `sp-rtk-base-relay` library;
this context is about *describing* a base station and *proving* that the
description works before committing to it.

## Language

### Configuration

**Input profile**:
The persisted description of where RTCM comes from — a source kind plus its
settings. What the operator edits on the Input page.
_Avoid_: input config, source config

**Relay**:
The `sp-rtk-base-relay` library that performs the actual relaying. Never this
application.
_Avoid_: engine, backend, service

### Verification

**Verification**:
A dress rehearsal of the relay's own connect path, run against the values
currently in the form rather than against what is saved. Answers one question:
would Save and Start connect?
_Avoid_: test, connection test, probe

**Stage**:
One named, operator-meaningful step of a Verification. The stage names are a
single shared vocabulary — the UI, the logs, and the tests all use the same
ones, so that a failure can be described by *which step* failed rather than by
whatever error text the layer below produced. A step earns the name only if it
can actually fail: a step whose outcome is structurally fixed reports nothing
and teaches the operator to stop reading stages.
_Avoid_: step, phase, check

**Green**:
A Verification outcome meaning Save and Start will connect **and will
reconnect after the Bond is lost**. The second half is the load-bearing one: a
Bond already in place carries the connection whatever the configured PIN says,
so only a PIN exercised against a fresh Bond promises anything about the next
reboot or eviction. A Green is a promise with a short life: it expires on its
own and is void the moment any field is edited. It is never persisted.
_Avoid_: success, passed, verified, OK

**Warning**:
A stage that did not pass but does not void a Green, because failing on it
would produce a false Red. Two unlike cases share the outcome and must not
share wording: a receiver that is **silent** because it is mid-survey — the
motivating case, and benign — versus one that is **answering with something
that is not RTCM**, which is benign only while a receiver is still emitting
NMEA or a boot banner, and otherwise means the wrong device was reached. The
second is weaker evidence of a working configuration than the first, and says
so in as many words.

**Red**:
A Verification outcome meaning the connect path failed, always attributed to
the Stage that failed.
_Avoid_: failure, error

### Bluetooth pairing

**Bond**:
BlueZ's stored pairing record for a device. Survives restarts, and is what
makes a later pairing attempt a no-op rather than a real PIN exchange.
_Avoid_: pairing, paired state

**Proven PIN**:
A PIN that has actually been exercised against a Bond with a specific device,
as opposed to one merely typed into the form. Durable, and scoped to the
device it was proven against: it is not a Green and does not expire with one.
_Avoid_: verified PIN, valid PIN, known-good PIN

**Force-repair**:
Discarding an existing Bond so that a PIN can be exercised against a fresh
one. The only way to make a PIN Proven when a Bond already exists, and
destructive enough that it is done only when the configured PIN is unproven.
_Avoid_: re-pair, reset pairing

**Stranded**:
A device left with no Bond by a Verification that removed the old one and
could not build a new one. Named because it is damage the application caused,
not a neutral state a device may innocently be in. Recovered by correcting the
PIN and running the Verification again, which simply pairs.
_Avoid_: unbonded, unpaired, broken pairing
