# A Corrected survey-in pulls through its own Relay instance, and the app writes the Frames to the receiver

A Corrected survey-in feeds a Correction source's RTCM into the receiver
while the application averages the receiver's RTK positions. The averaging
needs the receiver port, and the application already holds that port
exclusively for the whole survey: the driver opens it with `TIOCEXCL` and
`flock`, and every read and write goes through one lock. So the port stays
with the application. The *pull* is a second, survey-scoped `RelayEngine`.
Its input is the Correction source, it has no destinations, and it has one
Frame subscriber. The application takes each Frame from that subscriber and
writes it, whole and unfiltered, to the receiver through the driver, under
the same lock as the position polls. The Relay pulls; the application
pushes.

This instance belongs to the Corrected survey-in, not to `RelayService`. It
starts when the survey starts and stops on completion, cancel or device
disconnect. It is invisible to "relay running", the dashboard, metrics and
Signal Quality, all of which keep meaning the operator's Relay: where this
base's own RTCM goes.

## Considered options

- **A Relay destination that writes to a receiver port.** Rejected: the
  Relay would have to own the port, leaving the application unable to read
  the positions it averages.
- **An NTRIP client inside sp-rtk-base, with no Relay.** Rejected: it
  duplicates the Relay's NTRIP client input (v1/v2, reconnects, conformance
  fixes) and would drift from it.
- **Letting `RelayService` own both instances.** Rejected: a shared running
  flag would make the Survey page refuse to talk to the receiver ("relay is
  running") during the very survey that needs it.

## Consequences

- **This is consistent with relay ADR 0003, not an exception to it.** The
  Relay still delivers nothing on the operator's behalf: the application
  does the delivery. Don't "fix" this by making the receiver a Relay
  destination.
- The driver interface gains a write path for correction bytes. Today
  nothing writes raw bytes to a receiver.
- The Correction source's instance must be stopped before any handoff to
  the operator's Relay.
