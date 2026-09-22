# Research: what does UBX-MON-COMMS report on a ZED-F9P, and can it identify which receiver port the console is on?

> **Resolved: 2026-09-22** · Ticket [#151](https://github.com/rodenj1/sp-rtk-base/issues/151) · Map [#150](https://github.com/rodenj1/sp-rtk-base/issues/150) · Type: research (AFK, paper-only — no hardware touched)
> **Verdict: the mechanism in map #150 decision 2 is sound on paper, at our exact protocol version — but one detail of the `portId` encoding will silently break the obvious implementation, and the "attribute by an exact byte delta" half of the plan is weaker than the "attribute by which port moved at all" half.** `UBX-MON-COMMS` is documented `Periodic/polled` at interface version **27.31** (our reference receiver's `PROTVER`), it is *not* output by default so polling is the only cost-free way to read it, and `rxBytes` is documented as "Number of bytes ever received" per port. The trap: u-blox's own port table gives **UART2 the `portId` `0x0201`, not `0x0200`** — an equality test against a tidy `bank << 8` table matches I2C, UART1, USB and SPI and silently never matches UART2, which is exactly the port this app writes RTCM to. Decode by the **high byte**, not by equality — as u-blox's own `ubxlib` does. Two further traps recorded below: `txErrors.outputPort` looks like a blessed "which port am I on" answer and is decoded by our pyubx2, but it does not exist at 27.31 and reads `N/A` on real F9P hardware even at 27.50; and gpsd's decoder has the `0x0201` mapping wrong today, so other implementations are not a safe cross-check.

---

## 1. Question (from the ticket)

Map #150 decision 2 proposes deriving the console's receiver port at runtime: poll `UBX-MON-COMMS`,
write a known number of bytes, poll again, and take the port whose `rxBytes` advanced by that much
as the console's. Three premises under it were unverified:

1. What the `portId` U2 actually encodes on Gen9, and whether it matches Gen8 `CFG-PRT` numbering.
2. Whether `MON-COMMS` exists and is POLL-able at `PROTVER=27.31` on a ZED-F9P.
3. Whether `rxBytes`/`txBytes` are cumulative, per-port, prompt, and whether they wrap.

Plus: is `UBX-MON-MSGPP` better or complementary, and does u-blox document a blessed way for a host
to learn which port it is talking to?

This ticket answers *what the protocol offers on paper*. Issue
[#153](https://github.com/rodenj1/sp-rtk-base/issues/153) is the bench verification.

## 2. Sources, and why these ones

Everything in sections 3–7 is quoted from two u-blox PDFs whose stated applicability covers our
reference rig exactly. That matters more than usual here, because the ticket rightly warns that a
Gen8 answer which does not hold on Gen9 is worse than no answer.

| Ref | Document | Number / rev | Applicability |
|---|---|---|---|
| **[ID-27.31]** | *u-blox F9 HPG 1.32 — Interface Description* | UBX-22008968 R01, 02-May-2022 | Title page: "describes the interface (**version 27.31**) of the ZED-F9P". **This is our exact `PROTVER`.** |
| **[IM-R16]** | *ZED-F9P — Integration manual* | UBX-18010802 R16, 30-Oct-2024 | "applies to the following products" table lists **ZED-F9P-04B-01 / FW HPG 1.32** — the firmware whose protocol version is 27.31. |
| **[ID-27.11]** | *u-blox ZED-F9P Interface Description* | UBX-18010854 R07, 10-Jul-2019 | Title page: "v27.11". Used **only** to show the message is stable across 27.x. |
| **[IM-R08]** | *ZED-F9P — Integration manual* | UBX-18010802 R08, 02-Jun-2020 | Superseded. Cited **only** in §4.1 to explain a contradictory table still circulating. |
| **[pyubx2]** | pyubx2 1.3.0, as pinned and installed in this repo | `.venv/lib/python3.10/site-packages/pyubx2` | The decoder we would actually use. |
| **[ubxlib]** | u-blox's **own** C library, `gnss/src/u_gnss_info.c` | GitHub `u-blox/ubxlib`, master | First-party vendor *code* — the only non-document u-blox statement on `portId`. |
| **[CAP-27.50]** | A real ZED-F9P MON capture shipped in pyubx2's test suite, `tests/pygpsdata-MON.log` | — | Self-identifies as `MOD=ZED-F9P`, `FWVER=HPG 1.50`, `PROTVER=27.50`. **Observed hardware behaviour**, one minor version above ours. |

Note on the ticket's framing: it names **UBX-18010854** as the interface description to trust. That
document exists and was read, but it is **v27.11** (R07, 2019) — it is *not* the document for our
receiver. The interface description that carries **27.31** is **UBX-22008968** (the u-blox F9 HPG
1.32 document; u-blox renumbered the F9 interface description between HPG 1.13 and HPG 1.32).
Both were checked and they agree on everything below; where they differ it is noted. **Anything
citing UBX-18010854 alone for a 27.31 claim is one protocol revision off** — worth knowing before
someone re-derives this from the older PDF.

---

## 3. Availability and poll-ability at 27.31 — **yes, documented, no caveats**

[ID-27.31] §3.14.1, "UBX-MON-COMMS (0x0a 0x36)":

> Message `UBX-MON-COMMS` — Communication port information
> **Type  Periodic/polled**
> Comment  Consolidated communications information for all ports. The size of the message is
> determined by the number of ports that are in use on the receiver. **A port is only included if
> communication, either send or receive, has been initiated on that port.**

The message-type legend, [ID-27.31] §1 (p. 21), defines that type:

> **Periodic/polled**  Messages that are output in regular intervals **and can be polled**. E.g. UBX-NAV-PVT.

and §3.5.2 "UBX polling mechanism" gives the mechanism:

> The UBX protocol is designed so that messages can be polled by sending the message required to the
> receiver but **without a payload** (or with just a single parameter that identifies the poll
> request). The receiver then responds with the same message with the payload populated.

**Established fact:** `MON-COMMS` is supported and POLL-able at 27.31, by a zero-payload
`0x0A 0x36` frame. It is not output-only.

It is also stable across the 27.x range we care about. [ID-27.11] §5.13.1 carries an explicit
firmware line the newer document dropped:

> Firmware  Supported on: • **u-blox 9 with protocol version 27.11**
> Type  **Periodic/Polled**

The payload layout is byte-identical between 27.11 and 27.31. The only substantive change is the
`protIds` legend: 27.11 lists "0: UBX, 1: NMEA, 2: RTCM2, 5: RTCM3, 256: No protocol reported"
(the `256` is a typo in a `U1[4]` field), 27.31 lists "0: UBX, 1: NMEA, 2: RTCM2, 5: RTCM3,
**6: SPARTN**, **0xFF**: No protocol reported".

**One counter-claim you may run into, and it is wrong.** gpsd's own documentation
(`www/ubxtool-examples.adoc`) states: *"Gen9 does not officially support the UBX-MON-COMMS message,
and Gen10 removes it completely."* Both halves are contradicted by u-blox's documents — [ID-27.11]
says in as many words "Supported on: u-blox 9 with protocol version 27.11", and M10 (protVer 34.10)
still lists `MON-COMMS` as `Periodic/polled` — and by gpsd's *own driver*, which polls the message
whenever protVer ≥ 27. Treat the sentence as stale; the real point buried in that page is the port
*numbering* problem (§4.3), not message availability.

### 3.1 It is not emitted by default, so polling is the whole cost

[ID-27.31] §6.2 configuration-item table, default column:

| Key | ID | Default |
|---|---|---|
| `CFG-MSGOUT-UBX_MON_COMMS_I2C` | `0x2091034f` | `0` |
| `CFG-MSGOUT-UBX_MON_COMMS_UART1` | `0x20910350` | `0` |
| `CFG-MSGOUT-UBX_MON_COMMS_UART2` | `0x20910351` | `0` |
| `CFG-MSGOUT-UBX_MON_COMMS_USB` | `0x20910352` | `0` |
| `CFG-MSGOUT-UBX_MON_COMMS_SPI` | `0x20910353` | `0` |

**Established fact:** rate 0 on every port out of the box — the receiver will not volunteer
`MON-COMMS`. **Implication (inference):** port identification needs no `CFG-VALSET`, no durable
config change, and no cleanup — a poll and a read. That is a meaningfully smaller blast radius than
anything else in `apply_receiver_config`, and it means identification can run on connect without
touching receiver state.

---

## 4. The `portId` encoding — **the one finding that will bite an implementation**

[ID-27.31] does *not* define the encoding. It defers:

> `8 + n·40`  `U2`  **`portId`** — "Unique identifier for the port. **See section Communications
> ports in the integration manual for details.**"

The integration manual delivers. [IM-R16] §3.8 "Communication interfaces" (p. 47):

> The ZED-F9P provides UART1, UART2, SPI, I2C and USB interfaces for communication with a host CPU.
> […] **The following table shows the port numbers reported in the UBX-MON-COMMS messages.**

> | Port no. | UBX-MON-COMMS portId | Electrical interface |
> |---|---|---|
> | 0 | `0x0000` | I2C |
> | 1 | `0x0100` | UART1 |
> | – | `0x0101` | Reserved |
> | – | `0x0200` | Reserved |
> | **2** | **`0x0201`** | **UART2** |
> | 3 | `0x0300` | USB |
> | 4 | `0x0400` | SPI |
>
> *Table 27: Port number assignment*

**Established fact, and the headline of this ticket: UART2 is `0x0201`. `0x0200` is explicitly
listed as Reserved.** The encoding is *not* a clean `port_no << 8`. Four of the five real ports
have a zero low byte; UART2 does not.

### 4.1 Why the wild has two contradictory tables — and which one is right

**R08 of the same integration manual (UBX-18010802, 02-Jun-2020) §3.6 Table 21 prints different
values**, and third-party code written against it is wrong on every port:

> | Port # | Port # in MON-COMMS | Electrical interface |
> |---|---|---|
> | 0 | `0x0000` | I2C |
> | 1 | `0x0001` | UART1 |
> | 2 | `0x0102` | UART2 |
> | 3 | `0x0003` | USB |
> | 4 | `0x0004` | SPI |
>
> *Table 21: Port number assignment* — **superseded, do not use**

Every R08 value is the R16 value **byte-swapped**: `0x0001`↔`0x0100`, `0x0102`↔`0x0201`,
`0x0003`↔`0x0300`, `0x0004`↔`0x0400`. **Inference, but a checkable one:** UBX is a little-endian
protocol, and `portId` is a `U2`. The wire bytes for UART2 are `01 02`; read little-endian as the
protocol requires, that is **`0x0201`** (R16). R08 tabulated the same wire bytes read
**big-endian**, i.e. in the order they appear on the wire. R16's values are the ones a
specification-conformant `U2` parse — including pyubx2's — actually produces.

**So: trust [IM-R16]. Any table showing `0x0102` for UART2 is a revision-R08 artefact**, and its
presence is a reliable tell that a snippet was written against the 2020 manual. This also means
"I found a different table on the internet" is not evidence against §4 — check which revision it
came from first.

### 4.2 `0x0201` is ZED-F9P-specific — do not generalise across Gen9

Two other u-blox parts assign UART2 differently: the **NEO-D9C** integration manual
(UBX-21031631 R04) lists **`0x0200`** with no Reserved rows, and the **ZED-X20P** integration manual
(Table 35) likewise uses **`0x0200`**, dropping `0x0101`/`0x0201` entirely. **Established fact**,
and a pointed one: `0x0201` is a property of *this module's* port table, not of Gen9 or of
`MON-COMMS`.

Real tooling already branches on it — PyGPSClient keys its table on `("F9", 0x0201)` vs
`("X20", 0x0200)`, and SparkFun's RTK Everywhere firmware picks `0x0201` or `0x200` at runtime from
the detected module. **Implication for us:** the high-byte decode rule in §4.3 survives every part;
a hard-coded `0x0201` literal would not. Since `hardware_identity` already resolves the model, a
future non-F9P receiver is a table entry, not a rewrite — but the high-byte rule means we probably
never need one.

### 4.3 Why this is a trap and not a curiosity

The ticket's own hypothesis — "high byte is the port bank and the low byte the port number, giving
values like 0x0000, 0x0100, 0x0200, 0x0300, 0x0400" — is **refuted for UART2 specifically**. An
implementation built from that guess would use a literal lookup:

```python
# WRONG — silently never matches UART2 on a ZED-F9P
_PORT_BY_ID = {0x0000: "I2C", 0x0100: "UART1", 0x0200: "UART2", 0x0300: "USB", 0x0400: "SPI"}
```

and would fail *closed and silently* on exactly the port this app cares most about: UART2 is one of
the two RTCM data-link ports the reference rig configures (`fake.py:291-292`, and the built-in
profile). A `dict.get()` miss reads as "unknown port", which routes straight into map #150's
decision-3 fallback — the app would conclude the hardware said no when the hardware said UART2.

**Decode by the high byte:**

```python
# Correct for every row of Table 27
_PORT_BY_BANK = {0: "I2C", 1: "UART1", 2: "UART2", 3: "USB", 4: "SPI"}
name = _PORT_BY_BANK.get(port_id >> 8)
```

`0x0201 >> 8 == 2 == UART2`, and this also absorbs the reserved `0x0101` / `0x0200` rows into the
right bank rather than dropping them.

**u-blox's own code confirms both the table and the rule.** [ubxlib] `gnss/src/u_gnss_info.c` — a
first-party source, and the only place u-blox states this outside a PDF table:

```c
// The encoding of the port number in this message is _different_
// to that in UBX-CFG-PORT ... which is AFTER endian conversion:
//
// 0 ==> 0x0000 I2C
// 1 ==> 0x0100 UART1
// 2 ==> 0x0201 UART2
// 3 ==> 0x0300 USB
// 4 ==> 0x0400 SPI
//
// This is because there are additional UARTs internal to the
// GNSS device which need to be accounted for.  The ones listed
// above are those that may be connected to a host MCU, but note
// that others (e.g. 0x0101) may appear in the output of
// UBX-MON-COMMS, which we will ignore.
port = ((uint32_t) port) << 8;
if (port == (((uint32_t) U_GNSS_PORT_UART2) << 8)) {
    port++;
}
```

So the high-byte reading is **not** merely our inference: u-blox implements exactly `bank << 8`
with a `+1` special case for UART2, says in so many words that the encoding differs from
`CFG-PRT`, explains *why* `0x0201` is odd (internal UARTs), and confirms that undocumented IDs such
as `0x0101` do appear on real hardware and should be ignored rather than treated as errors.

**And the mistake is not hypothetical — it has already shipped twice.** The Rust `ublox` crate
decoded `portId` as a 0–5 index ([ublox-rs/ublox#288](https://github.com/ublox-rs/ublox/issues/288),
fixed in #290): "only I2C decodes correctly and every other port falls through to
`PortId::Unknown`". And **gpsd still has it wrong today** — `drivers/driver_ubx.c`, verified
verbatim on master:

```c
static const struct vlist_t vtarget[] = {
    {0, "DDC"}, {1, "UART1"}, {2, "UART2"}, {3, "USB"}, {4, "SPI"},
    {0x100, "UART1"},       // MON-COMMS
    {0x200, "UART2"},       // MON-COMMS   <-- Reserved on ZED-F9P
    {0x300, "USB"},         // MON-COMMS
    {0x400, "SPI"},         // MON-COMMS
    {0, NULL},
};
```

No `0x201` entry, and `0x200` — Reserved per Table 27 — mapped to UART2. **Consequence for us:
"check what another decoder does" is not a safe cross-check for this field.** The reference
implementations disagree, and the widely-trusted one is wrong for our exact part.

### 4.4 Relationship to Gen8 `CFG-PRT` numbering

The "Port no." column — 0=I2C/DDC, 1=UART1, 2=UART2, 3=USB, 4=SPI — is **identical to the Gen8
`CFG-PRT` `portID` numbering**, and that numbering is still visible in [ID-27.31] itself, in the
deprecated `CFG-PRT` variants:

> `0  U1  portID` — "Port identifier number (**= 3 for USB port**)"
> `0  U1  portID` — "Port identifier number (**= 4 for SPI port**)"
> `0  U1  portID` — "Port identifier number (**= 0 for I2C (DDC) port**)"

**Established fact:** the *port numbers* did not change between Gen8 and Gen9. **What changed is
the field**: `CFG-PRT.portID` is a `U1` holding the port number; `MON-COMMS.portId` is a `U2`
holding a *different, wider* identifier whose high byte happens to equal that port number. Tooling
that assumes `MON-COMMS.portId` can be compared directly against a Gen8 `CFG-PRT` port number is
wrong for every port (it would need `>> 8`), and tooling that assumes the U2 is just the port
number zero-extended is wrong for all of them too (`0x0100 != 1`).

---

### 4.5 What a real ZED-F9P actually emits

Everything above is paper. There is one piece of *observed* Gen9 behaviour available without a
bench: [CAP-27.50], a live ZED-F9P capture shipped as a pyubx2 test fixture. Decoded here **with
this repo's own pinned pyubx2 1.3.0**, so it is reproducible in-tree:

```
MON-VER: hwVersion=00190000, FWVER=HPG 1.50, PROTVER=27.50, MOD=ZED-F9P

MON-COMMS raw: b5 62 0a 36 58 00 00 02 00 00 00 01 05 ff ...
  version=0  nPorts=2  mem=0 alloc=0 outputPort=0
  protIds = [0=UBX, 1=NMEA, 5=RTCM3, 255=none]
  portId_01 = 256 (0x0100, UART1)  txBytes=18620  rxBytes=0     msgs=[0,0,0,0]
  portId_02 = 768 (0x0300, USB)    txBytes=13105  rxBytes=1937  msgs=[123,0,0,0]

MON-MSGPP: the only non-zero counter in the whole 120-byte payload is msg4_01 = 123
```

Four things this establishes as **observed fact on real ZED-F9P hardware** (at 27.50 — one minor
version above ours, same part, same generation):

1. **`0x0100` = UART1 and `0x0300` = USB appear on the wire exactly as Table 27 says.** Two of the
   five rows are now confirmed by hardware, not just by a PDF. (UART2 is not exercised in this
   capture, so `0x0201` remains document-and-vendor-code only — that is the row #153 should
   prioritise.)
2. **`nPorts` was 2 on a five-port module.** UART2, I2C and SPI are simply absent, exactly as the
   "only included if communication has been initiated" clause predicts. Confirms §9.2(d): never
   index by position, never assume a port count.
3. **`MON-MSGPP`'s port indices resolve empirically.** The single non-zero MSGPP counter is
   `msg4_01 = 123`, and MON-COMMS in the same session reports `msgs_02_01 = 123` for `portId_02 =
   0x0300` (USB) with `protIds_01 = 0` (UBX). Same count, same protocol slot, same session — so
   `msg4` → `port3` → USB, i.e. `msgN` is port `N−1` in the **legacy 0-based port space**
   (0=I2C, 1=UART1, 2=UART2, 3=USB, 4=SPI), *not* the MON-COMMS `portId` space. This promotes §6's
   inference to a corroborated reading — though MON-MSGPP stays the weaker signal for the other
   three reasons in §6.
4. **`outputPort` read 0 ("N/A") on a message that was unambiguously emitted from one of those two
   ports.** See §7.1 — this is the finding that kills the one field that looks like a blessed
   answer.

**Caveat, stated plainly:** this is 27.50, not our 27.31, and it is a captured log rather than a
receiver we polled. It corroborates the documents; it does not replace #153.

---

## 5. `rxBytes` / `txBytes` semantics

[ID-27.31] §3.14.1 payload table, repeated group (offsets are **absolute**, with `n=0` giving the
first port at the base shown):

| Byte offset | Type | Name | Unit | Description (verbatim) |
|---|---|---|---|---|
| `8 + n·40` | `U2` | `portId` | – | Unique identifier for the port. See section Communications ports in the integration manual for details. |
| `10 + n·40` | `U2` | `txPending` | bytes | Number of bytes pending in transmitter buffer |
| `12 + n·40` | `U4` | `txBytes` | bytes | **Number of bytes ever sent** |
| `16 + n·40` | `U1` | `txUsage` | % | Maximum usage transmitter buffer during the last sysmon period |
| `17 + n·40` | `U1` | `txPeakUsage` | % | Maximum usage transmitter buffer |
| `18 + n·40` | `U2` | `rxPending` | bytes | Number of bytes in receiver buffer |
| `20 + n·40` | `U4` | `rxBytes` | bytes | **Number of bytes ever received** |
| `24 + n·40` | `U1` | `rxUsage` | % | Maximum usage receiver buffer during the last sysmon period |
| `25 + n·40` | `U1` | `rxPeakUsage` | % | Maximum usage receiver buffer |
| `26 + n·40` | `U2` | `overrunErrs` | – | Number of 100 ms timeslots with overrun errors |
| `28 + n·40` | `U2[4]` | `msgs` | msg | Number of successfully parsed messages for each protocol. The reported protocols are identified through the protIds field. |
| `36 + n·40` | `U1[8]` | `reserved1` | – | Reserved |
| `44 + n·40` | `U4` | `skipped` | bytes | Number of skipped bytes |

Header is 8 bytes (`version`, `nPorts`, `txErrors`, `reserved0`, `protIds[4]`); message length is
`8 + nPorts·40`.

**Established facts:**

- **Per-port**: yes — the counters live inside the per-port repeated group, keyed by `portId`.
- **Cumulative**: yes, and the wording is unusually strong — "**ever** sent" / "**ever** received",
  not "since last poll" and not "during the last sysmon period" (which *is* the wording used two
  rows away for `txUsage`/`rxUsage`, so the contrast is deliberate).
- **Width**: `U4`, 32-bit unsigned.

**Not found, in either interface description:** any statement about wrapping, rollover, saturation,
or reset. The documents simply do not address it.

**Inference (flagged as such):** a `U4` "bytes ever received" counter almost certainly wraps modulo
2³². At 57600 baud (≈5.76 kB/s sustained) 2³² bytes is roughly **8.6 years** of continuous
saturated input, so wrap is not a practical concern for a *receive* counter on the console's link.
`txBytes` on a port streaming RTCM continuously is the same order of magnitude. Either way, a
**delta** computed as `(after - before) & 0xFFFFFFFF` is wrap-safe for the short poll→write→poll
window regardless, so this uncertainty does not need resolving to implement decision 2.

**Not documented, and this one does matter:** *promptness*. Nothing in either document says when
`rxBytes` is sampled relative to a poll arriving, nor whether the poll request's own bytes are
counted before the response snapshot is taken. See §7.2.

---

## 6. `UBX-MON-MSGPP` — **worse, not better; useful only as a weak cross-check**

[ID-27.31] §3.14.7:

> Message `UBX-MON-MSGPP` — Message parse and process status
> Type  Periodic/polled
> Comment  **This message is deprecated in this protocol version. Use UBX-MON-COMMS instead.**

Fixed 120-byte payload:

| Offset | Type | Name | Description (verbatim) |
|---|---|---|---|
| 0 | `U2[8]` | `msg1` | Number of successfully parsed messages for each protocol **on port0** |
| 16 | `U2[8]` | `msg2` | … **on port1** |
| 32 | `U2[8]` | `msg3` | … **on port2** |
| 48 | `U2[8]` | `msg4` | … **on port3** |
| 64 | `U2[8]` | `msg5` | … **on port4** |
| 80 | `U2[8]` | `msg6` | … **on port5** |
| 96 | `U4[6]` | `skipped` | Number skipped bytes for each port |

Four reasons it is the weaker signal for our purpose:

1. **Explicitly deprecated at 27.31**, with `MON-COMMS` named as the replacement. Same for
   `MON-IO`, `MON-RXBUF` and `MON-TXBUF` — all four carry the identical deprecation line.
2. **It counts messages, not bytes.** "Successfully parsed messages" means a probe write only
   registers if it is a *well-formed* frame. Arbitrary padding bytes would land in `skipped`, not
   in `msgN`.
3. **16-bit counters.** `U2` per protocol per port wraps at 65535 messages — plausible to hit on a
   long-running rig, unlike `MON-COMMS`'s `U4` byte counters.
4. **The port indices are positional and never named.** The document says "port0" … "port5" and
   nowhere states which electrical interface each index is. **Inference:** they follow the "Port
   no." column of [IM-R16] Table 27 (0=I2C, 1=UART1, 2=UART2, 3=USB, 4=SPI). That is very likely
   right, but it is *unstated*, and it is precisely the sort of unchecked premise this repo has
   been burned by twice. `MON-COMMS` carries an explicit `portId` in-band and needs no such guess.

`UBX-MON-RXBUF` (0x0a 0x07) and `UBX-MON-TXBUF` (0x0a 0x08) carry the identical deprecation line
and are worse still: they index `U2[6]`/`U1[6]` arrays by **"target"**, a word the document never
defines and never maps to an interface. They report buffer occupancy, not cumulative bytes.

**Where it is genuinely complementary:** `MON-COMMS`'s own per-port `msgs[4]` array gives the same
parsed-message-count signal *with* an explicit `portId`, in the same message, from the same poll.
So the useful idea from `MON-MSGPP` — "count parsed UBX messages per port" — is available inside
`MON-COMMS` without adopting a deprecated message. See the probe-design note in §7.3.

---

## 7. Does u-blox document a blessed way to learn which port you are on? — **not at 27.31**

Searched both interface descriptions and the integration manual for any statement of the form "to
determine which port the host is connected to". **Nothing at our protocol version** — no
self-identification message, no "current port" query, no worked example. (u-blox *did* add a field
for exactly this at **27.50**, after ours; §7.1 explains why it still does not help.)

The three near-misses, and why none of them is the answer:

- **The receiver clearly knows.** [ID-27.31] §3.10.10, `UBX-CFG-MSG` (3-byte form): "Set message
  rate configuration for **the current port**" — field `rate`, "Send rate on **current port**". So
  the firmware tracks which port a command arrived on and acts on it. But this message *consumes*
  that knowledge; it never reports it, and it is deprecated past protocol version 23.01.
- **`$PUBX,41`** takes `portId` as an *input* parameter ("ID of communication port. See section
  Communication ports in the integration manual for details.") — the host must already know.
- **[IM-R16] §3.1.6.5** says "UBX-MON-COMMS message reports which data are received on which port",
  in the context of verifying SPARTN corrections arrive. That is the closest u-blox comes to
  endorsing the approach — it confirms `MON-COMMS` is the intended tool for "which port is data
  arriving on", but it is about *corrections*, not about the host locating itself, and it offers no
  procedure.

**Inference:** the poll→write→poll scheme in map #150 decision 2 is a *reasonable and idiomatic*
use of `MON-COMMS` — it is the message u-blox points at for per-port receive accounting — but it is
**our construction, not a documented recipe**. Nobody should cite u-blox as having blessed it.

### 7.1 `txErrors.outputPort` — the blessed answer exists, but not for us, and not anywhere

This qualifies the "not found" above, and it is the one place where a later firmware looks like it
might solve the whole ticket. It does not.

**u-blox did add exactly the field you would want** — but only at **protocol version 27.50**, in
the HPG 1.51 interface description, as three previously-reserved bits of `txErrors`:

> `bits 4…2  U:3  outputPort` — "Output port: **Reports the port from which this message was output
> from.** • 0 = N/A • 1 = I2C • 2 = UART1 • 3 = UART2 • 4 = USB • 5 = SPI"

That is a direct, first-party, self-identification mechanism, and it would make map #150 decision 2
unnecessary — no probe write, no delta, no ambiguity.

**Two independent reasons it does not help this app:**

1. **It does not exist at 27.31.** Verified by searching our own copies: the string `outputPort`
   appears **zero times** in [ID-27.31] and **zero times** in [ID-27.11]. At 27.31 the `txErrors`
   bitfield documents only `bit 0 mem` and `bit 1 alloc`; bits 2–4 are undocumented. Our reference
   receiver is 27.31.
2. **Even where it is documented, real F9P hardware returns 0 = N/A.** [CAP-27.50] is a ZED-F9P at
   **27.50** — the very version that introduced the field — and its `MON-COMMS` decodes to
   `outputPort = 0` on a message that was demonstrably emitted from either UART1 or USB (§4.5).
   Corroborated independently by the `satpulse` project's in-code note: *"The output port feature is
   documented for protocol version 40, but returns not available on F10N and F10T. It seems to work
   properly on protocol version 50 (X20 series)."*

**The trap this creates, and it is a live one:** pyubx2 1.3.0 **does** decode `outputPort` (§8), so
a `MON-COMMS` parse against our 27.31 receiver will cheerfully surface `outputPort=0` — from bits
the receiver's own protocol version does not define. A developer who spots that field in the parsed
output has every reason to think it is the easy answer. It is not: `0` means "N/A" at 27.50 and
means *nothing at all* at 27.31. **Do not branch on it.**

**Consequence for the map:** upgrading the rig's firmware is **not** a shortcut past decision 2.
The field only works on X20-class parts (protVer 50+), which is different silicon, not a newer
build of ours.

---

## 8. What pyubx2 1.3.0 contributes, and what it does not

Verified locally against the pinned install (`.venv/.../pyubx2`, `pyubx2.version == "1.3.0"`):

**The payload byte layout matches [ID-27.31] exactly** (with one bitfield caveat, below). `ubxtypes_get.py:1621` declares the
header (`version`, `nPorts`, `txErrors` bitfield, `reserved0`, `protgroup`×4) and a `portsgroup`
repeated `nPorts` times with `portId U2`, `txPending U2`, `txBytes U4`, `txUsage U1`,
`txPeakUsage U1`, `rxPending U2`, `rxBytes U4`, `rxUsage U1`, `rxPeakUsage U1`, `overrunErrs U2`,
`msggroup`×`U2`, `reserved1 U8`, `skipped U4` — 40 bytes per port, 8-byte header. A synthesised
two-port payload round-trips at exactly 88 bytes with correct field values for both ports,
confirming the stride.

**`MON-COMMS` is POLL-able through the pinned library.** `ubxtypes_poll.py:118` registers
`"MON-COMMS": {}` — a zero-payload poll. Constructing it yields the 8-byte frame:

```
b5 62 0a 36 00 00 40 ca
```

**Caveat: pyubx2's `txErrors` bitfield is from a *newer* revision than ours.** It declares
`{"mem": U1, "alloc": U1, "outputPort": U3}` — and `outputPort` is a **27.50** addition that does
not exist at 27.31 (§7.1). The byte layout is unaffected (`txErrors` is one `X1` either way), but
the decoded field is meaningless against our receiver. See §7.1 — this is a trap, not a bonus.

**`PROTIDS` matches 27.31, not 27.11.** `ubxtypes_decodes.py:234` has `{0: UBX, 1: NMEA,
2: RTCM2, 5: RTCM3, 6: SPARTN, 0xFF: "No protocol reported"}` — including SPARTN and the corrected
`0xFF`, i.e. the 27.31 legend.

**What pyubx2 does *not* give us — and this is the point:**

```
$ grep -rn "portId" .venv/.../pyubx2/
ubxtypes_poll.py:82:    "CFG-PRT": {"portID": U1},
ubxtypes_get.py:863:        "portID": U1,
ubxtypes_get.py:1642:                "portId": U2,
```

Three hits, no decode table. pyubx2 parses `portId` as a **bare `U2` integer and never interprets
it** — a real `MON-COMMS` parse surfaces `portId_01=256`, `portId_02=768`, not `"UART1"`/`"USB"`.
pyubx2 ships `PROTIDS`, `GRIDUTCGNSS`, `ASTATUS` and friends as decode dicts but has nothing for
port IDs.

**Consequence for the implementation:** the `portId` → `PortId` mapping is **ours to write and ours
to get right**, from [IM-R16] Table 27. There is no upstream table to lean on and therefore no
upstream bug to inherit — and no upstream check on the `0x0201` mistake. That table belongs in the
driver with a comment citing UBX-18010802 R16 §3.8 Table 27, because the next person to read
`0x0201` will assume it is a typo.

Note also that our `PortId` enum (`models/device_models.py:310`) covers only `UART1`/`UART2`/`USB`.
A ZED-F9P can legitimately report I2C (`0x0000`) and SPI (`0x0400`) in `MON-COMMS` — and the
reference profile *enables* SPI (`spi_enabled: true`). Identification must handle a `portId` that
decodes to a real port we have no enum member for, distinctly from one that decodes to nothing.

---

## 9. Bearing on map #150 decision 2

Decision 2: *poll `MON-COMMS`, write a known number of bytes, poll again; the port whose `rxBytes`
advanced by that much is the console's.*

### 9.1 What holds

- `MON-COMMS` exists, is POLL-able, and is documented at our exact `PROTVER` (§3). **Fact.**
- `rxBytes` is per-port and cumulative ("bytes ever received"), so a delta is meaningful. **Fact.**
- The message carries an explicit `portId` per port, decodable to an electrical interface via a
  published u-blox table. **Fact.**
- No receiver configuration is needed — the poll is free and leaves no state behind (§3.1). **Fact.**
- The pinned pyubx2 can both build the poll and parse the response. **Fact, verified locally.**

Nothing found invalidates decision 2. The hardware go/no-go in map #150 remains a real question,
but it is a question about *behaviour*, not about *whether the protocol offers the mechanism* — it
does.

### 9.2 What needs adjusting before it is implemented

**(a) The `portId` decode must be high-byte, not equality.** §4. This is the one finding that would
have produced a wrong implementation, and it fails in the direction that looks like a hardware
"no". If #153 runs against an equality-table implementation and the console is on UART2, the bench
will report "identification failed" and the map will take the decision-3 fallback for the wrong
reason. **Flagging this explicitly as a risk to the map's go/no-go.**

**(b) "Advanced by exactly N" is the fragile half of the plan; "was the only port that advanced" is
the robust half.** Three documented reasons:

1. **The poll request's own bytes are received bytes.** The second poll is an 8-byte frame arriving
   on the console's port, so it contributes to that port's `rxBytes` too. *Inference:* if the
   snapshot convention is consistent between the two polls, the observed delta is `N + 8`, not `N`
   — the first poll's 8 bytes cancel. But whether the snapshot is taken before or after the
   triggering frame is tallied is **not documented**, and a straight `delta == N` test fails under
   either convention.
2. **Promptness is undocumented.** Nothing states how soon after a byte arrives `rxBytes` reflects
   it, and `rxPending` ("bytes in receiver buffer") existing as a separate field suggests buffered
   bytes may be counted at a different stage than parsed ones. A poll issued immediately after the
   probe write may observe a partial delta.
3. **Other ports are not guaranteed quiet.** `rxBytes` counts *all* bytes received, including
   unparseable ones. On a rover or a rig with anything feeding UART2, a second port can advance
   concurrently.

The robust formulation: **exactly one port's `rxBytes` advanced by at least N** — and if more than
one advanced, take the largest delta only if it is unambiguous, else report ambiguity. That degrades
into map #150's "failed or ambiguous identification" fog item, which is where it belongs.

**(c) Make the probe a well-formed UBX message, not padding.** Then it advances *two* independent
counters on the same port in the same response: `rxBytes` (all bytes) **and** `msgs[i]` where
`protIds[i] == 0` (UBX parsed-message count, §5). Two corroborating signals from one poll, and the
message-count signal is immune to the `+8` ambiguity in (b)(1) because it counts whole frames. A
second `MON-COMMS` poll is itself a fine probe. **Inference**, but it costs nothing and turns a
single fragile equality into a cross-check.

**(d) Absence is informative, presence is not enumerable.** "A port is only included if
communication, either send or receive, has been initiated on that port" (§3) means `nPorts` is not
5, and a port the console has never touched may simply not appear. **Observed, not just
documented:** [CAP-27.50] reports `nPorts = 2` on a five-port ZED-F9P (§4.5). Code must not index by
position or assume a fixed port list — iterate the group and key on `portId`. It also means the
*first* poll may not list the console's port at all if the poll itself is what initiates
communication; the second poll will. Allow for up to 7 entries, since the Reserved `0x0101` and
`0x0200` banks do appear on real hardware ([ubxlib]: "others (e.g. 0x0101) may appear ... which we
will ignore") — skip unknown banks, never error on them.

**(e) Do not cross-check the decode against other libraries.** §4.3: gpsd's C driver maps `0x200`
(Reserved) to UART2 and has no `0x201` entry, and the Rust `ublox` crate shipped a 0–5 index decode
until 2024. [IM-R16] Table 27 and [ubxlib] are the sources; a third-party table agreeing or
disagreeing proves nothing.

**(f) Ignore `txErrors.outputPort`, however tempting it looks in the parsed output.** §7.1.

### 9.3 What this ticket cannot settle

All of these are [#153](https://github.com/rodenj1/sp-rtk-base/issues/153)'s, and none is answerable
from the documents:

- Whether the poll request's own bytes appear in the same response's `rxBytes` (§9.2 b1).
- How promptly `rxBytes` advances after a write (§9.2 b2).
- Whether a ZED-F9P at HPG 1.32 answers a `MON-COMMS` poll on **USB** the same way it does on a
  UART — nothing suggests otherwise, nothing confirms it.
- Whether `rxBytes` wraps or saturates at 2³² (§5) — untestable on a bench in any case, and
  immaterial if deltas are masked.
- The actual `portId` the reference rig reports for the console's link. Table 27 says what it
  *should* be; #153 says what it *is*.

---

## 10. Summary table

| Sub-question | Answer | Status |
|---|---|---|
| `portId` encoding on Gen9 | I2C `0x0000`, UART1 `0x0100`, **UART2 `0x0201`**, USB `0x0300`, SPI `0x0400`; `0x0101`/`0x0200` Reserved | **Fact** — [IM-R16] §3.8 Table 27 |
| Decode rule | High byte = port number; low byte a sub-index | **Confirmed by u-blox's own code** — [ubxlib] implements `bank << 8` with a `+1` for UART2 |
| Do other decoders agree? | **No** — gpsd maps Reserved `0x200` to UART2 and lacks `0x201`; `ublox` crate shipped a 0–5 index bug | **Fact**, both verified verbatim |
| Real-hardware confirmation | `0x0100`=UART1 and `0x0300`=USB seen on a ZED-F9P at 27.50; `nPorts=2` on a 5-port module | **Observed** — [CAP-27.50], decoded with our pinned pyubx2 |
| Differs from Gen8 `CFG-PRT`? | Port *numbers* identical (0=I2C…4=SPI); the *field* differs — `U1` number vs `U2` identifier | **Fact** — [ID-27.31] deprecated `CFG-PRT` variants |
| Supported at 27.31? | Yes | **Fact** — [ID-27.31] §3.14.1 |
| POLL-able? | Yes, `Periodic/polled`, zero-payload `0x0A 0x36` | **Fact** — [ID-27.31] §3.14.1 + §3.5.2 |
| Output by default? | No — all five `CFG-MSGOUT-UBX_MON_COMMS_*` default to 0 | **Fact** — [ID-27.31] §6.2 |
| `rxBytes`/`txBytes` cumulative & per-port? | Yes — "bytes ever received"/"ever sent", inside the per-port group | **Fact** — [ID-27.31] §3.14.1 |
| Width | `U4` (32-bit) | **Fact** |
| Wrapping | Not documented anywhere | **Not found**; wrap-at-2³² is inference, ~8.6 yr at 57600 baud |
| Promptness | Not documented | **Not found** — #153 |
| `MON-MSGPP` better? | No — deprecated at 27.31, counts messages not bytes, `U2` counters, port indices unnamed | **Fact** — [ID-27.31] §3.14.7 |
| `MON-MSGPP` port indices | `msgN` = port `N−1` in legacy 0-based space (msg4 → USB) | **Observed** — [CAP-27.50], §4.5 |
| `MON-MSGPP` complementary? | Superseded — `MON-COMMS.msgs[]` gives the same signal *with* an explicit `portId` | **Inference** |
| Blessed way to learn your own port? | **None at 27.31.** `txErrors.outputPort` was added at **27.50** and would be exactly that — but it is absent from our protocol version *and* reads `0 = N/A` on real F9P hardware at 27.50 | **Fact** (absent at 27.31, verified) + **observed** (reads N/A) |
| Is a firmware upgrade a shortcut? | **No** — `outputPort` only works on X20-class parts (protVer 50+) | **Inference** from [CAP-27.50] + satpulse |
| pyubx2 1.3.0 support | Parses and polls correctly; **no `portId` decode table** — mapping is ours to write | **Fact**, verified locally |
| Conflicting `portId` tables in the wild | [IM-R08] (2020) prints every value byte-swapped (`0x0102` for UART2); superseded by R16 | **Fact**; byte-order explanation is **inference** |
| Does `0x0201` generalise to other Gen9 parts? | **No** — NEO-D9C's manual lists `0x0200` for UART2 | **Fact** — UBX-21031631 R04 |
