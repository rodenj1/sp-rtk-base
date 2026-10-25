# Network links

What the dashboard's **Network** card shows, and how to read it. The terms
(**Network link**, **Uplink**, **Wi-Fi link strength**, and the reading
states **Not supported**, **Disconnected** and **No reading**) are defined
in [`CONTEXT.md`](../CONTEXT.md#network-links). The same readings are
exported on `/metrics`; see the
[metric reference](monitoring/README.md#network-links).

## Where the readings come from

A background sampler reads every Network link every 2 s: each physical
Ethernet or Wi-Fi interface the base station has, including a USB adapter
plugged in while the app runs. Virtual interfaces (a VPN, Docker bridges)
and mobile-broadband modems aren't Network links. The sampler reads only
what the kernel already knows (sysfs and netlink). It never scans for
Wi-Fi networks, never asks NetworkManager anything and never reconnects, so
watching the network doesn't disturb it. The card is redrawn from the
latest sample on the dashboard's 2 s refresh; opening the page doesn't
trigger a read.

## The card

**Header.**

- **Uplink:** which interface the base station's outbound internet traffic
  actually leaves by. See [The Uplink](#the-uplink).
- **"Link health only: whether the caster and Portal are reachable shows
  under Destinations."** See [Link health isn't
  reachability](#link-health-isnt-reachability).
- **Top right:** "Updated N s ago". Once the last full sample is more than
  6 s old (three missed samples) it turns into a warning, "No reading for
  N s.". Before the first sample it reads "Waiting for the first reading.".

**One column per Network link,** listed Uplink first, then Ethernet, then
Wi-Fi, each by name. The heading names the kind and the interface
("Wi-Fi · wlan0", "Ethernet · eth0"), and the Uplink carries an **Uplink**
badge. Beneath it:

| Row | Shows |
|---|---|
| State | **Connected**, **Disconnected** or **No reading**, with the reason beside it |
| Wi-Fi link (Wi-Fi) | Wi-Fi link strength, **Good** / **Marginal** / **Poor**, with the latest dBm beneath |
| Network (Wi-Fi) | the Wi-Fi network's name (SSID), with the access point's BSSID beneath |
| Band (Wi-Fi) | 2.4 or 5 GHz, with the frequency in MHz beneath (a frequency outside both, such as 6 GHz, shows —) |
| Bitrate (Wi-Fi) | ↑ the rate of the last frames sent, ↓ received, in Mbit/s |
| Speed (Ethernet) | the negotiated speed in Mbit/s, with full or half duplex beneath |
| Address | the IPv4 address, with the IPv6 address beneath |
| Traffic | ↓ received and ↑ sent per second, measured between the last two samples |
| Errors / drops | the kernel's error and drop totals, with link changes (and, for Wi-Fi, failed sends) beneath |

On a phone the columns stack.

**Wi-Fi link strength** is how strongly the base station hears the access
point it's associated with. The verdict comes from the lower median of the
last 10 s of dBm readings, so a single dip doesn't flip it and a tie leans
to the weaker level:

| Verdict | dBm |
|---|---|
| Good | -67 or above |
| Marginal | -68 to -77 |
| Poor | -78 or below |

The dBm beneath the verdict is the latest reading, not the median. Any gap
in the readings (a disconnect, a failed read) clears the 10 s window, so
the verdict starts afresh. It's judged only while the base station is a
Wi-Fi client, never for the setup access point. It is not Signal Quality,
which is about the satellites, nor Bluetooth link strength.

**Link changes** count each time a link goes down or comes up, and each
time Wi-Fi roams to a different access point (a BSSID change while
connected). They start at 0 when the app starts. A link that keeps
dropping shows here even when it looks Connected right now.

**Failed sends** are frames the access point never acknowledged, as the
Wi-Fi driver counts them. They reset with each association. A climbing
count is a second sign of a struggling radio, besides a low dBm.

## Not supported, Disconnected and No reading

A reading with no current value is never shown as an old value or a zero.
There are three reasons it can have none:

- **Not supported:** the hardware or its driver never gives this reading,
  so its row isn't shown at all, rather than as a permanent "—". For
  example, an Ethernet adapter that doesn't report its speed has no Speed
  row, and a Wi-Fi driver that doesn't count failed sends has no failed
  sends. A reading that turns up later while the link is up starts showing
  from then on.
- **Disconnected:** the link is down, so readings that only exist while
  it's connected (Wi-Fi link strength, SSID, band, bitrate, Ethernet speed)
  show "—". The reason says why:
  - "No cable." for Ethernet without carrier;
  - "Wi-Fi is disconnected." for Wi-Fi that isn't associated;
  - "The setup access point is on." while wlan0 runs the base station's
    own setup access point instead of joining the site's Wi-Fi.
- **No reading:** the base station can't take the reading right now, or
  its last one is more than 6 s old. It's judged per source, so a Wi-Fi
  adapter that stops answering ("The Wi-Fi adapter gave no reading.")
  leaves the Ethernet column, the addresses and the counters current. In
  No reading every value reads "—", except the error, drop and link-change
  totals, which keep their last value so the history isn't lost.

## The Uplink

The Uplink is the interface the kernel would send the base station's
outbound IPv4 internet traffic through, right now. The sampler asks the
kernel to route a socket towards a public address (`1.1.1.1`) and reads
back which local address it picked. No packet is sent. This follows the
routing table as it really is, VPNs and policy routing included, so:

- **An Ethernet cable isn't assumed to carry the traffic.** With Ethernet
  and Wi-Fi both connected, the Uplink is whichever one the routes
  actually use.
- **"No Uplink"** (red) means the kernel has no route to the internet at
  all, even though links may be Connected: a DHCP or routing problem
  rather than a radio or cable one.
- **"wg0 (not a Network link)"** (or `tun0`, `tailscale0`) means the
  traffic leaves through a VPN. The card doesn't work out which physical
  link carries the VPN; no column gets the Uplink badge.
- **The setup access point isn't a way out.** While it's on, wlan0 is
  Disconnected ("The setup access point is on."), because the base station
  isn't on the site's Wi-Fi. Any Uplink then has to be another link, such
  as Ethernet; with no cable it's normally "No Uplink".
- **"No reading"** (amber) means the route check itself failed.

The Portal agent's primary LAN address, which the base station reports to
the Portal when it Checks in, follows the same rule, so the Portal and the
dashboard agree.

## Link health isn't reachability

A Connected link with Good Wi-Fi link strength says the base station's own
hop to the access point or switch is healthy. It says nothing about
whether the internet, DNS, the caster or the Portal can be reached beyond
it. Whether corrections are actually getting through shows under
**Destinations** on the dashboard. A good link with failing Destinations
points upstream (the router, the ISP, the caster); a Poor or flapping link
points at the base station's own network.

## Limitations

- **Drops shorter than about 2 s may go uncounted.** The sampler looks
  every 2 s, so a link that drops and recovers between two samples never
  shows as Disconnected and isn't counted in link changes. The kernel's
  error and drop counters still count what happened to traffic.
- **The Uplink is judged for IPv4 only.** Where IPv6 traffic leaves isn't
  checked.
- **The Pi's built-in Wi-Fi (brcmfmac) gives no tx retries, beacon loss
  or noise,** so none of these is shown. Failed sends and dBm are the
  signs of a struggling radio it does give. Measuring noise would need a
  survey that retunes the radio, which the sampler never does.
- **The Wi-Fi link strength thresholds are provisional.** -67 dBm is the
  usual floor for a reliable link; they may be retuned once readings at
  weak Wi-Fi link strength from a bench Pi are in.

## For whoever maintains the systemd unit

The sampler reads Wi-Fi details over netlink (`AF_NETLINK`) from the main
app's unit, `sp-rtk-base.service`. Any future `RestrictAddressFamilies=`
there must include `AF_NETLINK`, and `PrivateNetwork=` must never be
added: either silently leaves every link with no reading. The sampler must
not run in the Portal agent, whose unit blocks netlink; the agent only uses
the Uplink rule, which needs nothing beyond `AF_INET`.
