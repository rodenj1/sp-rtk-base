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
would the connection it rehearses work? For an Input profile, that is whether
Save and Start would connect. For a Correction source, it is whether a
Corrected survey-in started now would receive corrections it can use.
_Avoid_: test, connection test, probe

**Stage**:
One named, operator-meaningful step of a Verification, or of a Bluetooth
Console link connect (Pair, Connect, Identify). The stage names are a
single shared vocabulary — the UI, the logs, and the tests all use the same
ones, so that a failure can be described by *which step* failed rather than by
whatever error text the layer below produced. A step earns the name only if it
can actually fail: a step whose outcome is structurally fixed reports nothing
and teaches the operator to stop reading stages.
_Avoid_: step, phase, check

**Green**:
A Verification outcome meaning the connection it rehearses will work. A
Green is a promise with a short life: it expires on its own and is void the
moment any field is edited. It is never persisted.
For a Bluetooth Input profile, Green means Save and Start will connect **and
will reconnect after the Bond is lost**. The second half is the load-bearing
one: a Bond already in place carries the connection whatever the configured
PIN says, so only a PIN exercised against a fresh Bond promises anything
about the next reboot or eviction.
For a Correction source, Green means a Corrected survey-in started now would
receive RTCM 3 Frames from it.
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
A Correction source has a different Warning. Its Frames arrive, but none of
them carries the reference station's position, which may only mean the caster
sends it rarely. A Correction source that is silent, or that sends bytes that
never form a Frame, is Red, not a Warning: unlike a receiver, a correction
stream has no benign reason to be quiet.

**Red**:
A Verification outcome meaning the connect path failed, always attributed to
the Stage that failed.
_Avoid_: failure, error

### Signal

**Signal Quality**:
How well the receiver's antenna is hearing the satellites, judged from
what any receiver can report rather than from one vendor's fields. Its
verdict is **Good**, **Marginal** or **Poor**. It is about reception only:
whether RTCM is reaching its destinations is a separate concern.
_Avoid_: signal health, signal strength, GPS health, antenna health

**Signal Snapshot**:
The C/N0 of every signal the receiver would put in its corrections at one
epoch, meaning signals from satellites it is using, above its elevation
mask, each tagged with its constellation, satellite and band. The only thing
a Signal Quality verdict is judged from, whatever receiver or stream supplied
it, so the same sky earns the same verdict before and after the Relay takes
over the port. Satellites the receiver tracks but leaves out of its
corrections (low ones, or ones it isn't using) are not part of it.
_Avoid_: observation (survey-in's word for a position sample), measurement, sample

**Band strength**:
The mean C/N0 of the four strongest signals in one band group of a Signal
Snapshot: L1 (L1, E1, B1) or L2 (L2, E5b, B2). Judged on the strongest
signals rather than all of them, because weak signals from low or partly
blocked satellites are normal at any site.
_Avoid_: average C/N0, signal level, SNR

**Usable satellite**:
A satellite in a Signal Snapshot with at least one signal at 35 dB-Hz or
more. Counts sky coverage, where Band strength counts antenna and cable
health.
_Avoid_: satellites tracked, satellites used (the receiver's own navigation
count, which is a different number)

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

### Baud detection

**Detection**:
An operator-initiated sweep of candidate baud rates on one serial port,
answering one question: what rate is the receiver listening at? It
discovers a value rather than promising an outcome — unlike a Verification,
which rehearses the relay's connect path. Nothing detects on its own:
Connect never falls back to it, and no page runs it on load. A detected rate
is **not a Green** — it does not expire, because a baud rate is a property
of the wiring, not of a Bond that can be evicted underneath it.
_Avoid_: auto-detect, autobaud, probe, scan

**Candidate**:
One baud rate a Detection tried, together with what happened when it did:
the receiver *answered*, or the port carried *bytes but no answer*, or it
was *silent*. Never the bare rate on its own — a Candidate without its
verdict is just a number. The middle verdict is the informative one: at
exactly one rate, with silence elsewhere, it means the link is at that rate
and the receiver is not accepting commands on that port.
_Avoid_: attempt, trial, step, stage

**Rate-indifferent**:
A port whose baud setting the kernel accepts and discards, so that every
Candidate answers. A directly USB-connected receiver is the case: the u-blox
configuration database has a baud key for each UART and none for USB,
because the USB interface is not a UART. A property of the port, not a
verdict on the Detection that met it — reporting it is an observation about
the hardware, not a failure to find anything.
_Avoid_: baud-agnostic, ignores baud, fake serial port

### The console's own link

**Console link**:
This application's own connection to the receiver, the one every console
page (Survey, Fixed base, GPS config, Signal) talks through. Its kind is
**Serial** (a host serial device, including a receiver's own USB port seen
as one) or **Bluetooth** (a Bluetooth console link to a module wired to one
of the receiver's UARTs). Distinct from the Input profile, which is where
the Relay reads RTCM from, even when both reach the same Bluetooth device.
_Avoid_: connection, transport, device connection

**Console port**:
The receiver port this application's own Console link is attached to — as
the receiver sees it (UART1, UART2, USB), never the host end we opened.
`/dev/ttyUSB0` (or a Bluetooth module) is where we are; UART1 is where the
receiver hears us. Learned by asking the receiver once per connection, and kept until
disconnect; it may be **unknown**, which is a state to handle rather than
an error.
_Avoid_: connected port, management port, host port

### The saved base

**Saved base**:
The base configuration the receiver keeps through a reset or power cycle:
a fixed base, or base mode off. Only committing a finished Survey-in, or
setting or restoring a fixed base, changes it. A Survey-in that ends any
other way (cancelled, aborted, or cut short by a reset) is abandoned, and
the receiver goes back to the saved base, because nothing about an
uncommitted Survey-in is ever saved.
_Avoid_: persisted config, flash config, stored base

### Corrected survey-in

**Stall**:
A Corrected survey-in whose observation time has stopped growing: no
Observation since the start, or since the last one. In a Corrected survey-in
an Observation is an RTK Fixed solution that has held for the settling time
(30 s) on fresh corrections (at most 10 s old). A short stall only pauses
the survey; after a minute the page warns, and after ten minutes the survey
aborts, naming why: **no corrections** (no fresh ones are arriving, with the
source's last error) or **no fixed** (fresh corrections are in use, but the
receiver reaches only Float). An aborted survey commits nothing and never
falls back to a plain Survey-in.
_Avoid_: timeout, hang

**Fixed jump**:
In a Corrected survey-in, a sudden step of more than 5 cm from the last
Fixed solution, lasting 3 samples: the receiver has moved to a different
Fixed solution, one of them wrong, without reporting a break in Fixed. The
survey discards its average and settles again, so the two are never mixed
into one fixed base. A slow drift (a long baseline's) is not a jump: it is
averaged.
_Avoid_: false fix (either side may be the right one), outlier

### Updating the base

**Update**:
The operator replacing the installed application and Relay with the newest
stable release, then restarting so the base runs it. Always started by a
person; a base never updates itself. Refused while a Survey-in runs or the
Console link is connected; a running Relay only drops its corrections
briefly and comes back as it would after a reboot.
_Avoid_: upgrade, self-update, patch

**Available update**:
A stable release newer than the one running. Pre-releases and yanked
releases never are.
_Avoid_: new version, latest version (the running one may already be it)

**Release notes**:
What changed in every release after the running one, up to and including
the Available update: the application's and the Relay's, read before
pressing Update.
_Avoid_: changelog (the file), version notes, what's new

**Host setup**:
The system files only the installer lays down: the service units and the
pieces that let the operator start an Update. Update cannot change them, so
a release that needs newer Host setup asks for the installer to be re-run
once instead of offering the button.
_Avoid_: plumbing, provisioning (that's the network's)

**Rollback**:
An Update undone because its new version failed to start, putting the
previous application and Relay back. Automatic; the operator never starts
one.
_Avoid_: downgrade, revert
