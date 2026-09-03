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
whatever error text the layer below produced.
_Avoid_: step, phase, check

**Green**:
A Verification outcome meaning Save and Start will connect **right now**. A
Green is a promise with a short life: it expires on its own and is void the
moment any field is edited. It is never persisted.
_Avoid_: success, passed, verified, OK

**Warning**:
A stage that did not pass but does not void a Green, because failing on it
would produce a false Red. A receiver that is mid-survey and not yet emitting
is the motivating case.

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
