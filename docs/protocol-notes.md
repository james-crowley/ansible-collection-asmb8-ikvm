<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Protocol notes: iUSB virtual media and IVTP console framing

The normative wire-format reference for this collection. AMI has never
published a specification for either protocol described here, so every fact
below carries its own provenance — where it came from, and how confident this
collection is in it — rather than being stated as if it were vendor
documentation. Read [`docs/capability-matrix.md`](capability-matrix.md) for the
tiered confidence model this file's provenance tags map onto, and
[`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md) for
the underlying dated hardware record.

**Provenance tags used throughout this file:**

- **[decompiled vendor client]** — a local, non-redistributed decompilation of
  the ASUS/AMI JViewer client retrieved from the target hardware itself. This
  is the vendor's own client for *this exact board and firmware*, so where it
  disagrees with any other source below, it wins. See [NOTICE](../NOTICE)
  entry 5 for exactly which classes were examined and what was derived from
  each; the binaries and decompiled output are never redistributed.
- **[live capture]** — observed directly against the target ASUS Z10PE-D16 WS
  / ASMB8-iKVM board, firmware 1.14 (aux 1.14.2), on 2026-08-08. See
  [`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md).
- **[MIT reference implementation]** — `BadCoder1337/rd450x-console`, a
  third-party Go client for a different vendor's AMI MegaRAC board (a Lenovo
  RD450X, firmware 2.36). Same underlying AMI codebase, different vendor and
  firmware generation — useful for structure and cross-checking, not
  authoritative for this board's own quirks.
- **[Wireshark dissector]** — `samozy/iusb`'s `iusb.lua`, an independent,
  much older reverse-engineering artifact (floppy device class only). Facts
  only (byte offsets are not copyrightable); no code was taken from it. Cited
  as third-party corroboration precisely because it was authored without
  reference to any of the other sources here.
- **[unverified]** — stated explicitly whenever a fact has not been confirmed
  by a live capture, even if it follows logically from something that has.
- **[asp-corpus 2026-08-10]** — a corpus of 54 real ``.asp`` response bodies,
  one per endpoint, captured from the target board's own web UI on
  2026-08-10 (firmware 1.14, aux 1.14.2 — the same board and firmware as the
  **[live capture]** tag above, captured two days later by a different
  method: reading the web UI's own RPC traffic rather than a dedicated
  capture session). Copied into `tests/unit/fixtures/asp/` — see that
  directory's own `README.md` for redaction details — and parsed by
  `plugins/module_utils/webvar.py`, whose own docstring cites the same
  corpus for the parser-level facts §9 below does not repeat.
- **[save-action capture 2026-08-10]** — a distinct, later capture from the
  same board and firmware, made while the web UI performed an actual save
  action rather than a plain page load — the only way to observe this
  board's `POST`-only endpoints and their request bodies, which the
  **[asp-corpus 2026-08-10]** page-load capture never exercised. Backs §10
  below, including its NTP-specific write-convention subsection (a later
  save action against `setntpcfg.asp`/`setdatetime.asp`, same day). Four of
  its response bodies were added to `tests/unit/fixtures/asp/`:
  `getselentries_post_lasteventid24.txt` and
  `getsessioninfo_post_servicebit4.txt` (POST-parameterized reads), plus
  `setntpcfg_write.txt` and `setdatetime_write.txt` (write replies, backing
  `asmb8_ntp`) — see that directory's own `README.md` for why none of the
  four are folded into the 54-file count above.

## 1. The 32-byte iUSB packet header

Every iUSB frame — auth, ACK, SCSI request, SCSI response, kill — begins with
this 32-byte header. All multi-byte fields are **little-endian**
**[decompiled vendor client]**, confirmed independently by the Go reference's
own `binary.LittleEndian` framing **[MIT reference implementation]**.

| Offset | Length | Field | Notes |
|---|---|---|---|
| 0 | 8 | `signature` | ASCII `"IUSB"` followed by four spaces (`49 55 53 42 20 20 20 20`) |
| 8 | 1 | `major` | `1` on every frame this collection sends. On the auth **ACK**, the real board returns `0`, not `1` — **[live capture]** |
| 9 | 1 | `minor` | `0` on send. Also `0` on the ACK — same as the "normal" value here, so this field alone does not reveal the ACK oddity |
| 10 | 1 | `headerLen` | `32` on send. On the ACK, the real board returns `0`, not `32` — **[live capture]** |
| 11 | 1 | `checksum` | This collection computes it as `(-sum(bytes 0..31)) & 0xFF` with the checksum byte itself zeroed first, so the receiver's sum over all 32 header bytes is `0 (mod 256)`. **Empirically unenforced by the BMC**: this collection has booted a real ISO using this scheme, and its own parser does not validate an incoming checksum either — see below |
| 12 | 4 | `dataPacketLen` | Payload length, in bytes, following this header (u32 LE) |
| 16 | 1 | `serverCaps` | Not interpreted by this collection |
| 17 | 1 | `deviceType` | `5` for every device class (CD-ROM, floppy, hard disk) — see §3. The **server** ORs `0x80` into its own outgoing frames, so a request frame from the BMC is observed as `0x85` (`0x80 | 0x05`) — **[live capture]** |
| 18 | 1 | `protocol` | `1` |
| 19 | 1 | `direction` | `0` = client→server; `128` (`0x80`) = server→client |
| 20 | 1 | `deviceNumber` | Not interpreted by this collection |
| 21 | 1 | `interfaceNumber` | Not interpreted by this collection |
| 22 | 1 | `clientData` | Not interpreted by this collection |
| 23 | 1 | `instance` | Device-slot instance number; `0` is correct for a board with a single virtual CD-ROM slot — the only configuration this collection has validated |
| 24 | 4 | `sequenceNumber` | u32 LE. Echoed **verbatim** by the receiver on its response — this, not the SCSI envelope's `cmdctr` field (§2), is what pairs a request with its response |
| 28 | 4 | `reserved` | **Not always zero on real server frames** — one capture showed `bc 38 02 bf` — **[live capture]**. This collection's parser (`Header.parse` in `plugins/module_utils/iusb.py`) never validates these bytes, and nothing built on top of it should either |

**Sequence-number pairing, not `cmdctr` pairing.** It is tempting to use the
SCSI envelope's own `cmdctr` field (§2) to match a response to its request.
Do not: a live capture observed `cmdctr` values of 3, 7, then 16 for three
consecutive commands — non-sequential, and not usable as a correlation key —
**[live capture]**. The header's `sequenceNumber` is sequential and is what
this collection actually keys off.

## 2. The SCSI request/response envelope (inside the payload)

The payload following the 32-byte header, for every SCSI-carrying frame, has
this structure. Offsets below are relative to the **start of the payload**
(i.e. header offset 32 plus the value in this table).

| Payload offset | Length | Field | Notes |
|---|---|---|---|
| 0 | 4 | transfer/data length | u32 LE. On a **request**, the requested transfer length; on this collection's **response**, overwritten with the number of SCSI data-in bytes appended — **[decompiled vendor client]**, cross-checked live |
| 4 | 4 | `cmdctr` | Command counter. **Non-sequential** — observed 3, 7, 16 in one live session. Not used for request/response pairing (see §1) — **[live capture]** |
| 8 | 1 | marker byte | `0x01` observed on live request envelopes. Not independently interpreted by this collection's own client, which only reads/writes the fields it actually needs — **[live capture]**, informational |
| 9 | 1 | **opcode** | SCSI CDB byte 0 (`OPCODE_OFFSET` in `plugins/module_utils/iusb.py`), or one of the AMI control opcodes in §4 when the value is `≥ 0xF0` |
| 10.. | — | remainder of the SCSI CDB | See §5 for the specific CDB layouts this collection speaks |
| 13 | 1 | (CDB byte 4) `loej` | Only meaningful for opcode `0x1B` (`START STOP UNIT`) — see §5. **Exact equality to `2`** signals eject, not a bitmask — **[decompiled vendor client]** |
| 25 | 4 | **response length** | u32 LE. This is the field the BMC actually forwards to the host as "how many bytes follow" — **load-bearing**, confirmed by live traffic across `READ(10)` for LBA 0, 1, 16-18, the El Torito boot catalog at LBA 4660, and multi-block reads up to 16 blocks (32 KiB): every one of those exchanges round-tripped correctly only once this offset — not merely offset 0 — carried the response length. **[live capture]** |
| 30 | 1 | `connectionStatus` (ACK) / token-type byte (auth request) | See §3 |
| 31.. | — | session token text (auth request) / owner-IP text (ACK, when the device is already redirected) | See §3 |

A response frame is built by taking the request's own envelope bytes,
appending the SCSI data-in payload, then patching offsets 0 and 25 with the
appended length (`CDROMDevice._build_response` in `plugins/module_utils/iusb.py`).

## 3. The authentication handshake

**[decompiled vendor client]** for the opcode/offset facts below;
**[live capture]** for the exact byte values observed on the target board.

### Client → server: auth request

- 32-byte header (§1) with `direction=128`, `dataPacketLen` set to the
  payload length below.
- Payload is **128 bytes** for a web-session token (type `0`), or **240
  bytes** for an SSI token (type `1`) — this collection only ever sends the
  128-byte, web-session-token form.
- Payload offset 9: opcode `0xF2` (`OP_AUTH`).
- Payload offset 30: token-type byte — `0` for a web-session token, `1` for
  SSI.
- Payload offset 31 onward: the session token text — the 16-character
  `-kvmtoken` minted by fetching `/Java/jviewer.jnlp` (see
  `plugins/module_utils/asp.py`'s `allocate_media_session`), **not** the
  output of a bare `getsessiontoken.asp` call, which returns an empty
  `STOKEN` on this board — **[live capture]**.

### Server → client: ACK

- Header oddity, **[live capture]**: `major`, `minor`, and `headerLen` all
  come back as `0`, not the expected `1`/`0`/`32`.
- Payload offset 9: opcode `0xF1` (`OP_REDIRECT_ACK`).
- Payload offset 30: `connectionStatus`.
  - `1` (`CONN_OK`) — accepted; the session is now authenticated.
  - Any other value — rejected. `5` and `8` are the values the decompiled
    JViewer sources use for "device already in use", ported from that source
    **[decompiled vendor client]**, but **no live capture has independently
    confirmed either value against this specific board** — **[unverified]**.
    This collection treats *any* non-`1` status as
    `ErrorClass.BMC_BUSY` (`interpret_ack` in `plugins/module_utils/iusb.py`),
    on the reasoning that this board's media slot is single-occupancy with no
    server-side timeout (see [`README.md`](../README.md#known-limitations)),
    so a rejected auth overwhelmingly means a stale session still holds the
    slot rather than a wire-format bug — not on the specific numeric value.
  - When rejected because the device is already redirected, payload offset 31
    onward may carry the current owner's IP address as trimmed ASCII text
    (`other_ip` in `plugins/module_utils/iusb.py`).

### Kill

- Opcode `0xF6` (`OP_KILL_REDIR`), server → client, no payload of interest.
  Ends the serve loop with no response sent — **[decompiled vendor client]**
  (`IUSBSCSI.OPCODE_KILL_REDIR=246`).

### Other observed control opcodes

A live capture observed the BMC issuing `0xF1` (ACK, expected) and **`0xF3`**
during normal operation — the latter was previously undocumented in this
collection's own analysis and its meaning is **[unverified]**. Every AMI
control opcode `≥ 0xF0` that this collection's `CDROMDevice.handle` receives
and does not specifically recognise gets a harmless, silent envelope echo
rather than raising — see §4.

## 4. AMI control opcodes vs. SCSI opcodes

Both ride in the same payload-offset-9 byte. Values `0xF0`–`0xFF` are AMI's
own redirection-control opcodes, not SCSI:

| Opcode | Meaning | Direction |
|---|---|---|
| `0xF1` | Redirection ACK | server → client |
| `0xF2` | Session-token auth | client → server |
| `0xF3` | Observed during normal operation; meaning **[unverified]** | server → client |
| `0xF6` | Kill redirection | server → client |

`0xF1`/`0xF2` are consumed by the handshake before the SCSI serve loop ever
starts; `0xF6` is intercepted by the serve loop itself before it reaches the
device emulation. Any other value `≥ 0xF0` that somehow reaches
`CDROMDevice.handle` gets an empty, harmless response rather than the
"unhandled opcode" warning a genuinely unrecognised SCSI opcode gets — this is
believed unreachable in practice (the decompiled dispatcher shows a hard error
for anything outside the six SCSI opcodes below, not a silent ack), and is
flagged in `plugins/module_utils/iusb.py` as a live-hardware verification
target, not a confirmed fact.

## 5. The six CD-ROM SCSI/MMC opcodes

**[decompiled vendor client], authoritative for this board**: disassembling
the vendor's native SCSI dispatcher established that the CD-ROM device class
implements **exactly six** opcodes. Every other opcode — including `INQUIRY`,
`MODE SENSE`, `GET CONFIGURATION`, `REQUEST SENSE`, and every `WRITE` command
— reaches a hard error in the vendor's own code.

| Opcode | Name | Notes |
|---|---|---|
| `0x00` | `TEST UNIT READY` | Status-only; no data phase |
| `0x1B` | `START STOP UNIT` | See below for the eject encoding |
| `0x25` | `READ CAPACITY(10)` | Response: last LBA (4 bytes) + block size (4 bytes), **both big-endian**, 8 bytes total — byte-exact verified against a 1052-sector test image **[live capture]** |
| `0x28` | `READ(10)` | LBA at CDB bytes 2-5, transfer length (blocks) at CDB bytes 7-8, **both big-endian** |
| `0x43` | `READ TOC/PMA/ATIP` | This collection emits a minimal, single-data-track, formatted TOC |
| `0xA8` | `READ(12)` | LBA at CDB bytes 2-5, transfer length (blocks) at CDB bytes 6-9, **both big-endian** |

**`INQUIRY` (`0x12`) is never forwarded to the client.** The BMC's own
firmware answers it directly — **[decompiled vendor client]**. This
collection's `CDROMDevice` never receives, and therefore never needs to
handle, an `INQUIRY` request.

**Block size is 2048 bytes**, not 512 — this is a CD-ROM/MMC device, not a
disk.

### `deviceType` is always `5`

`deviceType` (header offset 17) is `5` for **every** device class this
firmware supports — CD-ROM, floppy, and hard disk alike. The decompiled
`FloppyRedir` and `HarddiskRedir` classes both call the same
`IUSBHeader.createCDROMHeader(...)` helper (hardcoding `deviceType=5`) for
their own auth packets — **[decompiled vendor client]**. The BMC infers which
device is being redirected from the **port number**, not this header field.
This corrects a "TBD, guessed" comment in the MIT reference implementation,
which had reused the CD-ROM value on a guess that turns out to be right, but
for a different reason than it assumed — **[MIT reference implementation]**,
corrected by **[decompiled vendor client]**.

### Eject is exact equality, not a bitmask

`START STOP UNIT` (`0x1B`) signals an eject request when payload offset 13
(CDB byte 4, the `loej` field) equals **exactly `2`**. The MIT reference
implementation instead uses a bitmask (`payload[13] & 0x03 == 2`); the
decompiled vendor client for this specific board uses exact equality
(`iUSBSCSI.Lba == 2`) — **[decompiled vendor client]**, which wins because it
is the vendor's own client for this exact board. A hypothetical `loej` byte
of `0x06` would read as an eject under the masked comparison but not under
this one. **No live capture has independently confirmed which byte value(s) a
real eject actually sends** — `0x02` is what the SCSI specification's own
`loej`/power-condition encoding implies, but this remains a live-hardware
verification target — **[unverified]**.

### Big-endian CDB fields inside a little-endian wrapper

Every multi-byte field inside the 32-byte iUSB **header** is little-endian
(§1). The LBA and transfer-length fields **inside the SCSI CDB itself** are
standard SCSI **big-endian** fields, unaffected by the little-endian framing
around them. This was proven, not assumed: the firmware requested LBA 16, 17,
then 18 in sequence while reading an ISO9660 volume descriptor and El Torito
boot record — a byte-swapped (little-endian) implementation of those same CDB
bytes would instead have asked for LBA 268,435,456 — **[live capture]**.

## 6. The on-demand listener behaviour

Before a JNLP fetch (`/Java/jviewer.jnlp`) allocates a session, the media and
KVM ports return TCP **RST** — reachable host, nothing listening. Immediately
after a JNLP fetch, they are bound. **[live capture]**:

| Port | Service | State before a session | State after |
|---|---|---|---|
| 5120 | cd-media (plaintext) | RST | open |
| 5122 | fd-media (plaintext) | RST | open |
| 5123 | hd-media (plaintext) | RST | open |
| 7578 | KVM (plaintext) | RST | open |
| 5124 | cd-media (secure) | RST | refused (observed with encryption disabled in the BMC's config) |
| 7582 | KVM (secure) | RST | refused (same caveat) |

A closed `cd_port` before any attach is normal, not an error condition. The
dedicated ports bind **even when the JNLP reports `-singleportenabled 1`** —
that argument tells the vendor's Java client to *prefer* single-port mode; it
does not stop the BMC from binding its own dedicated listeners regardless.
This is why this collection needs no HTTP CONNECT tunnelling to reach virtual
media on the target hardware today — **[live capture]**.

## 7. The IVTP KVM console header

The KVM channel (port 7578 plaintext observed) speaks a distinct 8-byte
framing, **not** the 32-byte iUSB header:

| Offset | Length | Field |
|---|---|---|
| 0 | 2 | `type` (u16 LE) |
| 2 | 4 | `pktSize` (u32 LE) |
| 6 | 2 | `status` (u16 LE) |

This layout is documented by the MIT reference implementation for a
*different vendor's* AMI MegaRAC board — **[MIT reference implementation]**.
Connecting to plaintext port 7578 on the target ASMB8 board yielded exactly 8
bytes as a greeting:

```
17 00 00 00 00 00 00 00
```

Parsed against the layout above, that is `type=0x0017`, `pktSize=0`,
`status=0` — a well-formed greeting that matches the independent, clean-room
implementation written against a different vendor's board. This is strong
evidence the underlying protocol is genuinely shared across MegaRAC-based
boards rather than only coincidentally similar — **[live capture]**, cross-checked
against **[MIT reference implementation]**.

### The handshake `asmb8_console` actually drives

`asmb8_console` was named `asmb8_redirection` until that name was split off
for a differently-behaved module (service-enablement reporting, not a
console session) before this collection's first release — see
`changelogs/fragments/` and `docs/asmb8_redirection.md`. Nothing about the
protocol facts below changed in that split.

`plugins/module_utils/ivtp.py` implements a full handshake beyond the bare
greeting, sourced from a local decompilation of this board's own
`com.ami.kvm.jviewer.kvmpkts.IVTPPktHdr`/`KVMClient` classes —
**[decompiled vendor client]** unless noted otherwise. **None of this has any
live-capture or unit-test coverage** — see
[`docs/capability-matrix.md`](capability-matrix.md) Tier 4 for the specific
open questions this raises.

1. BMC → client, unsolicited: greeting, opcode `23` (`SESSION_ACCEPTED`).
   Confirmed live as exactly the 8-byte header with no body — §7 above.
2. Client → BMC, optional: `GET_WEB_TOKEN` (opcode `21`), body = the raw
   `-kvmtoken` bytes, unpadded. Sent by this collection's client by default,
   because the decompiled client's closest-matching code path sends it — **not
   because the BMC has been confirmed to require it** — **[unverified]**.
3. Client → BMC: `VALIDATE_VIDEO_SESSION` (opcode `18`), body **324 bytes**:

   | Body offset | Length | Field |
   |---|---|---|
   | 0 | 1 | token type (`0` = web-session token) |
   | 1 | 129 | session token, zero-padded ASCII |
   | 130 | 65 | client IP address, zero-padded ASCII |
   | 195 | 129 | client username, zero-padded ASCII |

   **Deliberate divergence from the decompiled source**, flagged explicitly:
   the decompiled client writes this packet's header `pktSize` field as `332`
   (the packet's *total* wire length, header included), while every other
   packet-building method in the same decompiled class uses `pktSize` to mean
   the *body* length that follows the header (`324`, for this packet). This
   collection writes `324` — self-consistent with the rest of the protocol,
   and with this collection's own precedent in `iusb.py` for an analogous
   vendor-client inconsistency (a checksum byte, §1) — on the theory that the
   BMC likely parses this fixed-size packet by a hardcoded length rather than
   trusting the client's stated `pktSize`. **This has not been verified
   against real hardware and is the single biggest live-hardware verification
   target in this file** — **[unverified]**.
4. BMC → client: zero or more tolerated/discarded packets (LED state,
   encryption-status pushes, bandwidth probes, …), then
   `VALIDATE_VIDEO_SESSION_RESPONSE` (opcode `19`): body byte 0 is the status
   (`0`=`INVALID_SESSION`, `1`=`VALID_SESSION`, `2`=`KVM_DISABLED`,
   `3`=`INVALID_VIDEO_TOKEN`, `4`=`INVALID_CDROM_TOKEN`,
   `5`=`INVALID_FLOPPY_TOKEN`); an optional body byte 1 is a sub-status the
   decompiled client never names the meaning of.
5. Client → BMC, only on status `1`: `RESUME_REDIRECTION` (opcode `6`,
   bodyless) — this is what starts the video stream.
6. BMC → client, unsolicited, at any point: `STOP_SESSION_IMMEDIATE` (opcode
   `8`), whose own status/reason byte distinguishes a web logout (`7`), a KVM
   license expiry (`8`), the BMC's own server-side inactivity timeout (`9`),
   or another client requesting a KVM disconnect (`10`).

**Video framing.** Once resumed, video arrives as `VIDEO_FRAGMENT` (opcode
`25`) packets, each body-prefixed with a 2-byte little-endian fragment number:
bit `0x8000` marks the **last** fragment of a frame; the low 15 bits being `0`
marks the **first**. This convention is independently corroborated by the MIT
reference implementation's own read loop for a different board — one of the
strongest cross-checked facts in this file, **[decompiled vendor client]**
and **[MIT reference implementation]** agreeing independently. Concatenating
fragments in order between a first and a matching last yields one complete
frame's **raw, still-encoded** bytes — the AMI/ASPEED VQ+JPEG(DCT), optionally
RC4-obfuscated, video codec that would turn this into pixels is **not**
implemented by this collection. `asmb8_console`'s `capture=raw_frame`
saves exactly this undecoded byte stream; `capture=decoded_frame` is refused
outright (`error_class=unsupported_capability`) rather than approximated.

**Claimed KVM service capacity — unsourced.** `plugins/modules/asmb8_console.py`'s
own `DOCUMENTATION` states the KVM service allows 4 concurrent sessions with
an 1800-second server-side inactivity timeout. This number appears nowhere
else in this collection's sourced material — not in the decompiled client
analysis, not in a live capture — and is attributed there only to "the task
brief this collection was built against." Treat it as **[unverified]** until
a real source backs it; see
[`docs/capability-matrix.md`](capability-matrix.md) Tier 4.

**Nothing beyond the above has been decoded, and none of it has been proven
against real hardware.** No console frame has been captured, parsed, or
rendered from this board — see the [capability matrix](capability-matrix.md)'s
Tier 4 for exactly what remains open on the KVM/video side.

## 8. Single-port mode

The decompiled vendor client's transport layer is independent of its framing
layer, and single-port mode is implemented as **per-channel HTTP CONNECT
tunnelling over one shared HTTP connection, not multiplexing several devices'
framing over one socket** — **[decompiled vendor client]**. This collection
does not implement single-port mode at all: the target board's current
configuration exposes dedicated per-device ports (§6), and `asmb8_media`
speaks directly to the dedicated `cd_port` listener over a plain TCP socket
with no HTTP layer in that path. If a board's configuration is ever
encountered where only single-port mode is available, this fact is recorded
here so that work does not have to start from nothing — but no code in this
collection has been written against it, and no claim is made about how it
would behave.

## 9. The `.asp` WEBVAR/JSONVAR response format

**[asp-corpus 2026-08-10]** for everything in this section unless noted
otherwise. This is the response shape returned by this board's older
``*.asp`` RPC endpoints — ``getdatetime.asp``, ``getallsensors.asp``,
``getfwinfo.asp``, and every sibling of them — as opposed to the JNLP
document (§6-§8 above) or the narrow ``SESSION_COOKIE``/``STOKEN`` login
fields ``asp.py`` already parses with its own regexes. It is a JavaScript
object literal, not JSON (every one of the 54 samples uses single-quoted
keys and string values; zero use double quotes anywhere), which
``plugins/module_utils/webvar.py`` parses without ever calling ``eval`` — see
that module's own docstring for the full shape, its sourcing, and exactly
which corpus fixture backs which claim. This section records only the two
facts below, because both matter to more than just that parser.

### `getallservicescfg.asp` confirms the media session has no server-side reclaim timeout

`getallservicescfg.asp` reports a `SERVICE_TIMEOUT` field per service. For
`cd-media`, `fd-media`, and `hd-media` it is **4294967295** (`0xFFFFFFFF`) —
the ``u32`` "no timeout" sentinel — while `web` and `kvm` report **1800**
and `ssh` reports **600**. This is the BMC's **own stated configuration**,
not an inference from behaviour: it directly confirms what this collection's
`errors.py` (`ErrorClass.BMC_BUSY`'s docstring) and
[`README.md`](../README.md#known-limitations) had previously stated only as
something *observed* — that the single-occupancy virtual-media/KVM channel
has no server-side timeout that will reclaim an abandoned session. The web
and KVM *listener* services do have a timeout (1800s); the *media device
services* riding over the JNLP-allocated session do not. Cross-reference
this fact wherever this collection asserts the "no automatic reclaim" limit
from observed behaviour alone — it no longer has to be.

### `getfwinfo.asp`'s minor firmware revision is BCD-encoded

`getfwinfo.asp` reports `FirmwareRevision1: 1, FirmwareRevision2: 20`. 20
decimal is `0x14` hex — i.e. **BCD**-encoded, not a plain decimal minor
version — which is how this board arrives at reporting itself as firmware
"1.14": `FirmwareRevision1` (`1`) joined with `FirmwareRevision2` read as two
BCD digits (`0x14` → `"14"`), not as the decimal number twenty. This is a
fact about **this specific field's own
encoding**, observed on this one record, not a general rule that every
integer field in this response format needs decoding — every other field in
this same record (`DeviceID`, `DevRevision`, `IPMIVersion`, `CompletionCode`,
…) is a plain decimal integer, and `webvar.py`'s parser deliberately stays
generic and does not special-case this field: presenting the "1.14" a human
expects is a job for whatever future module reads `FirmwareRevision2`, not
for the shared parser. Do not "simplify" a future consumer of this field by
printing `20` as if it already were the minor version — it is not.

## 10. POST-based reads, CSRFTOKEN, and this collection's `.asp` write convention(s)

**[save-action capture 2026-08-10]** for everything in this section. Every
fact here concerns endpoints or headers that §9's page-load-only corpus never
exercised, because nothing in it ever submitted a form.

### Some `.asp` reads require `POST`, not `GET`

Two endpoints in this corpus return nothing useful over a bare `GET` and
require their selector submitted as a `POST` body instead:

| Endpoint | Selector field | Meaning |
|---|---|---|
| `getselentries.asp` | `WEBVAR_LASTEVENTID` | Return SEL entries **after** this record ID (the paged sibling of `getallselentries.asp`) |
| `getsessioninfo.asp` | `SERVICEBIT` | Return the active session(s) for the service this bit selects |

Both are read**s**, plain and simple, despite the method: the BMC's own
choice of `POST` for a query it could in principle have taken as `GET`
parameters is not evidence of a side effect, and neither endpoint's response
shape differs in any way from an ordinary `.asp` read's — see "The envelope
is unchanged" below. `plugins/module_utils/asp.py`'s `AspClient.post_webvar()`
is this collection's client method for this shape: a `POST` that is a read,
kept structurally separate from `AspClient.get_webvar()` (which remains
`GET`-only, permanently, by design) specifically so a genuine mutation can
never be reached by a caller reusing the read path "for just one more
`POST`" — see that method's own docstring for the full reasoning.

`getselentries.asp`'s one real capture (`WEBVAR_LASTEVENTID=24` against a log
holding exactly 24 entries) returned zero records — the **correct** answer
("nothing follows the newest entry that already existed"), not evidence of
failure; see `plugins/modules/asmb8_sel.py`'s `after_event_id` option and
DOCUMENTATION for how this collection surfaces that distinction to a caller.

`getsessioninfo.asp`'s `SERVICEBIT` reuses `getallservicescfg.asp`'s own
`SERVICEID` values: the captured `SERVICEBIT=4` is `cd-media`'s `SERVICEID`
in the §9 corpus's own `getallservicescfg.txt`. Two independently-captured
values agreeing is what makes that identity **sourced**, not assumed — but
it is sourced for exactly that one service. Applying the same identity to
any other service (`web`=1, `kvm`=2, `fd-media`=8, `hd-media`=16, `ssh`=32,
`telnet`=64, per `getallservicescfg.asp`'s own `SERVICEID` values) is an
inference from a pattern confirmed once, not a second capture per service —
see `plugins/modules/asmb8_sessions.py`'s `active_session_services` option
for how this collection both takes advantage of the pattern (deriving the
mapping live, rather than hardcoding a second copy of it) and flags the
inference honestly.

### The envelope is unchanged

Both endpoints' response bodies parse under the exact same
`WEBVAR_JSONVAR_<NAME>`/`HAPI_STATUS` envelope §9 describes, with no
different framing for having arrived via `POST` — `HAPI_STATUS` carries the
result the same way it does for every `GET` read, and
`plugins/module_utils/webvar.py`'s parser handles both new fixtures with no
changes of its own, which is itself the evidence: nothing about "this was a
POST" needed a different parser.

### CSRFTOKEN

Every `POST` this capture observed carries a `CSRFTOKEN` request header
**except** the login itself (`POST /rpc/WEBSES/create.asp`) — which matches
the vendor JS's own rule, verbatim, from `lib/xmit.js`:

```js
if (this.url.indexOf("WEBSES") == -1) {
    this.xmldoc.setRequestHeader("CSRFTOKEN", top.user.CSRFtoken);
}
```

The token itself comes back in the login response's own body, alongside
`SESSION_COOKIE` (see `tests/unit/fixtures/asp/create.txt`'s `CSRFTOKEN`
field) — that is what the vendor UI stores and replays, and what
`AspClient.login()` now harvests (falling back to leaving it unset if the
field is absent, never treating that as a login failure). A separate
`/rpc/getcsrftoken.asp` endpoint also exists but is not used, since it would
cost this serialized client an extra round trip against a web server already
known to wedge under concurrent load, for a value the login response already
carries.

**Correction (2026-08-11), retiring a claim this section got wrong.** This
section used to say that whether this firmware *enforces* CSRFTOKEN on any
request "is unverified", and specifically that "this collection's existing
`GET` reads send no such header and have always worked without it". That
was **wrong**, and it was wrong in a specific, instructive way: it
generalised "works without it" from a partial sample. The `GET` endpoints
this project happened to test by hand before [GitHub issue
#5](https://github.com/james-crowley/ansible-collection-asmb8-ikvm/issues/5)
(`getvmediacfg`, `getallservicescfg`, `getdatetime`, `getntpcfg`) simply
happen not to enforce the header — that is a fact about those four
endpoints, not about `GET` as a method. Issue #5 (2026-08-11, live hardware)
showed five *other* `GET` endpoints (`getalllancfg.asp`,
`getlanchannelinfo.asp`, `getdnscfg.asp`, `getnwbondcfg.asp`,
`checknwbond.asp`) answering an authenticated `GET` missing `CSRFTOKEN` with
a session-expired-looking HTML page (2,223 bytes, SHA-256
`7129528f34a2b230534e705ad8cb230cd1f5d4ae0362a9f9694c99b61f4c3427` in every
one of the five), and answering normally, every time, once the header was
attached.

**Enforcement is per-endpoint, not a blanket `GET`-vs-`POST` rule.**
`AspClient._headers()` now attaches `CSRFTOKEN` to `GET` as well as `POST`
(any non-`WEBSES` request, matching the vendor's own URL-based rule exactly
— see `AspClient._headers()`'s own docstring for the full history, including
the earlier, narrower GET-excluded version this replaced). Whether any given
`POST`/write enforces the header remains genuinely untested either way —
issue #5 exercised `GET` only — so that half of the original claim still
stands as **[unverified]**, just no longer generalised into a false claim
about `GET`.

**A structural detector for the failure shape, not a byte-length or digest
check.** `module_utils/asp.py`'s `looks_like_session_expired_html()`
recognises this HTML page by shape — an HTML document carrying a login/session
marker, with none of this format's own `WEBVAR_JSONVAR_` marker — rather than
by the exact 2,223 bytes or SHA-256 above, which are properties of one
firmware build's rendering of this one page and would silently stop matching
the moment a firmware revision changes so much as whitespace in it.
`AspClient.get_host_status()`/`get_webvar()`/`post_webvar()`/`set_webvar()`
all check for this shape before treating a response as legitimate, so a
caller gets a specific, named failure instead of either a generic parse
complaint or — the more dangerous failure issue #5 actually reported against
`asmb8_info(include_web_session=true)` — a confident wrong answer that
treated this HTML as a successful, if oddly-shaped, response.

### Three `set*.asp` endpoints are sourced so far. Two are now implemented; one is not

**[save-action capture 2026-08-10]** sourced this board's write shape on exactly three
endpoints. Naming all three here, in one place, is the point of this subsection: every future
`set*.asp` capture should be added to this list, not folded silently into prose elsewhere.

| Endpoint | Status | Backed by |
|---|---|---|
| `setvmediacfg.asp` | Sourced, **not implemented** — see below | This section's own capture |
| `setntpcfg.asp` | Sourced, **implemented** | `AspClient.set_webvar()`, `asmb8_ntp` |
| `setdatetime.asp` | Sourced, **implemented in `AspClient.set_webvar()` only** — no module calls it; see `asmb8_ntp`'s DOCUMENTATION for why | `AspClient.set_webvar()` |

#### `setntpcfg.asp` and `setdatetime.asp` — a sourced write convention, now implemented

A later save-action capture, same day, exercised NTP's own save action and observed both of these
endpoints POSTed together in the one save (reads first, for context):

```
GET  getdatetime.asp -> { 'SECONDS': 1786375523, 'UTCMINUTES': 480, 'TIMEZONE': 'GMT+8' }
GET  getntpcfg.asp   -> { 'SERVER_NAME1': 'pool.ntp.org', 'SERVER_NAME2': ' 192.0.2.10', 'NTP_STATUS': 1 }

POST setdatetime.asp   SECONDS=1786347240&UTCMINUTES=480&TIMEZONE=GMT%2B8&ISNTPENABLE=0
POST setntpcfg.asp     NEW_NTPSERVER_NAME1=pool.ntp.org&OLD_NTPSERVER_NAME1=pool.ntp.org
                       &NEW_NTPSERVER_NAME2= 192.0.2.10&ISNTPENABLE=0
```

Both replied in the same WEBVAR envelope, an empty record array, and `HAPI_STATUS:0` — see
`tests/unit/fixtures/asp/README.md`'s "Write replies" section for exactly what is and is not
sourced about the two reply fixtures this collection built from that description (the envelope
shape is sourced; the specific `WEBVAR_JSONVAR_<NAME>` text inside it is this repository's own
reconstruction of the established naming pattern, not verbatim captured bytes).

**The critical finding: setter field-name convention is per-endpoint, not collection-wide.**
`setvmediacfg.asp`'s capture above suggested `set<X>.asp` reuses `get<X>.asp`'s own field names.
This second capture both confirms and contradicts that in the same save action:

* `setdatetime.asp` *does* reuse `getdatetime.asp`'s own field names verbatim (`SECONDS`,
  `UTCMINUTES`, `TIMEZONE` all unchanged), plus a fourth field, `ISNTPENABLE`, that
  `getdatetime.asp` never returns at all.
* `setntpcfg.asp` does **not** reuse `getntpcfg.asp`'s field names at all: `SERVER_NAME1` becomes
  both `NEW_NTPSERVER_NAME1` *and* `OLD_NTPSERVER_NAME1`; `SERVER_NAME2` becomes
  `NEW_NTPSERVER_NAME2` only (**no** `OLD_NTPSERVER_NAME2` — see below); `NTP_STATUS` becomes
  `ISNTPENABLE`.

One inference from `setvmediacfg.asp` alone would have generalised the wrong way for
`setntpcfg.asp`. **The lesson generalises, the field names do not**: every future `set*.asp`
capture needs its own read of the actual field names on the wire — do not assume a new endpoint
follows either of these two conventions without checking.

**`OLD_NTPSERVER_NAME1` is sent; `OLD_NTPSERVER_NAME2` is not, even though `SERVER_NAME2` was the
field actually changing in this save.** This capture's own save action was toggling NTP off
(`ISNTPENABLE` moving to `0`); server 1 was not changing (`NEW` equals `OLD`, both
`pool.ntp.org`), yet an `OLD_NTPSERVER_NAME1` field was still sent. Server 2 carries its own
leading space through unchanged (`NEW_NTPSERVER_NAME2= 192.0.2.10`, matching
`getntpcfg.asp`'s `SERVER_NAME2` byte for byte) but has no `OLD` counterpart at all. This is
vendor-side inconsistency, not a transcription error, and `asmb8_ntp` follows it exactly: it
always sends `OLD_NTPSERVER_NAME1` and never invents an `OLD_NTPSERVER_NAME2`.

**`NTP_STATUS` (read) and `ISNTPENABLE` (write) are not confirmed to share an encoding.** The read
shows `NTP_STATUS: 1`; the write, from the same session, shows `ISNTPENABLE=0`. Nothing here
proves the two fields describe the same quantity, let alone the same encoding — one is a
read-only status field on `getntpcfg.asp`, the other a write-only field on two different setter
endpoints. `asmb8_ntp` maps `NTP_STATUS` to a boolean ("nonzero means enabled") and writes
`enabled=true` as `ISNTPENABLE=1` purely as a best-effort, explicitly-flagged inference — see its
DOCUMENTATION and `docs/asmb8_ntp.md` for the full caveat, and its `ntp_status_raw` return field
for the untranslated escape hatch.

**`setdatetime.asp` and `setntpcfg.asp` were POSTed together in the one save action, coupling
date/time and NTP-enable across two endpoints — `asmb8_ntp` deliberately manages only one side of
that coupling.** It writes `ISNTPENABLE` through `setntpcfg.asp` only, and never calls
`setdatetime.asp` at all, specifically to avoid resubmitting a live-read `SECONDS` value (which
would nudge the BMC's clock forward by whatever gap elapsed since the read) for a capability that
has nothing to do with the clock. Whether `setdatetime.asp`'s own copy of `ISNTPENABLE` tracks
`setntpcfg.asp`'s automatically on real firmware is unverified either way.

Transport is otherwise identical to the CSRFTOKEN section above: `Content-Type:
application/x-www-form-urlencoded`, a `CSRFTOKEN` header present on both POSTs. `AspClient`'s
generic write method for all of this is `set_webvar(endpoint, data, *, operation=None)` — named,
per its own docstring, so a write can never be reached by a caller reusing a read method for "just
one more POST"; it raises `errors.RemoteOperationError` on a non-zero `HAPI_STATUS`.

#### `setvmediacfg.asp` — a sourced, unimplemented write convention — record only, do not build

The original capture sourced this board's write shape on the virtual-media configuration endpoint:

```
POST /rpc/setvmediacfg.asp
Content-Type: application/x-www-form-urlencoded
CSRFTOKEN: <token>

V_NUM_FD=1&V_NUM_CD=2&V_NUM_HD=1&V_MEDIA_STATUS=1
```

replying in the same WEBVAR envelope, an empty record array, and
`HAPI_STATUS:0`. This was the first evidence for the "`set<X>.asp` takes the
field names `get<X>.asp` returns" pattern — now known, per the NTP capture
above, to be true for `setdatetime.asp` but false for `setntpcfg.asp`, i.e.
**not a convention that generalises across endpoints without its own
capture.** It remains recorded here purely as protocol knowledge for whoever
eventually sources it.

**Nothing in this collection implements this.** There is no `set_vmediacfg`
(or any other write) method on `AspClient`, no `state` option on
`asmb8_sel`/`asmb8_sessions`, and no code path anywhere under `plugins/`
that names `setvmediacfg.asp`. This section exists purely so the next
contributor sourcing a real write does not have to start from nothing — not
as a plan, a TODO, or an invitation to build one from this documentation
alone.

## Practical note for contributors

Per [`CONTRIBUTING.md`](../CONTRIBUTING.md): treat every byte layout and field
mapping in this file as normative. "Improving" one of these without a firmware
capture, a decompiled-client cross-check, or another sourced reference to back
the change is exactly the kind of unverified drift this project cannot afford
— the iUSB protocol has no public specification to fall back on if this
document quietly becomes wrong.
