<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Capability matrix: what is actually verified

A plain accounting of confidence levels, so a reader does not have to guess
which claims rest on real firmware evidence and which rest on reading someone
else's source code. Four tiers, and nothing here should be read as implying a
higher tier than it earns. **Being ruthless about Tier 4 is the point of this
document** — an overclaiming matrix is worse than no matrix at all.

1. **Verified against an authoritative source** — confirmed against a real
   firmware response, a decompiled vendor client for this exact board, a
   third-party reference implementation, or another project's own
   hardware-verified finding. Strong, but not the same as "tested on our
   hardware."
2. **Unit/mock tested** — exercised by this collection's own test suite
   against deterministic fixtures/mocks, never against the real BMC.
3. **Verified against real firmware** — observed directly against the one
   ASUS Z10PE-D16 WS / ASMB8-iKVM board this collection targets, firmware
   1.14 (aux 1.14.2), on 2026-08-08. Every row in this tier cites
   [`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md).
   **One machine, one firmware version. This is repeatability at best, not a
   compatibility guarantee.**
4. **Still unproven** — a specific, named list. Anything not confirmed
   elsewhere in this document belongs here by default.

This collection is **not** hardware-qualified. Tier 3 covers the individual
protocol facts and behaviours that were observed directly; it does **not**
cover a completed, unattended, end-to-end OS install, which has never
happened — see Tier 4.

## Tier 1: Verified against an authoritative source

| Claim | Source | Where used |
|---|---|---|
| The CD-ROM device class implements exactly six SCSI/MMC opcodes (`0x00`, `0x1B`, `0x25`, `0x28`, `0x43`, `0xA8`); every other opcode, including `INQUIRY`, reaches a hard error in the vendor's own dispatcher | Decompiled vendor JViewer client's native SCSI dispatcher, retrieved from the target hardware — see `NOTICE` entry 5 | `plugins/module_utils/iusb.py` (`CDROMDevice.handle`, `SCSI_*` constants) |
| `INQUIRY` is answered by BMC firmware itself and never forwarded to the client | Decompiled vendor client | `plugins/module_utils/iusb.py` (`CDROMDevice` never receives it — no `INQUIRY` handling exists) |
| `deviceType` is `5` for every device class (CD-ROM, floppy, hard disk) — the port, not this field, selects the device | Decompiled vendor client (`FloppyRedir`/`HarddiskRedir` both call `createCDROMHeader()`), correcting the MIT reference implementation's own "TBD, guessed" comment | `plugins/module_utils/iusb.py` (`DEVICE_CDROM = 5`) |
| Eject is exact equality on payload offset 13 (`== 2`), not a bitmask | Decompiled vendor client (`iUSBSCSI.Lba == 2`), overriding the MIT reference implementation's bitmask (`& 0x03 == 2`) | `plugins/module_utils/iusb.py` (`Packet.is_eject`) |
| Auth packet layout: opcode `0xF2` at payload offset 9, token at payload offset 31; ACK `0xF1` with status at payload offset 30; kill `0xF6` | Decompiled vendor client (`IUSBSCSI.OPCODE_EJECT`, `CDROMRedir.DEVICE_REDIRECTION_ACK`/`AUTH_CMD`, `OPCODE_KILL_REDIR`) | `plugins/module_utils/iusb.py` (`build_auth`, `interpret_ack`) |
| The 32-byte header field offsets and little-endian framing | Decompiled vendor client; independently corroborated by `samozy/iusb`'s Wireshark dissector, authored with no reference to the other sources here | `plugins/module_utils/iusb.py` (`Header.marshal`/`Header.parse`) |
| Single-port mode is per-channel HTTP CONNECT tunnelling, not multiplexing over one shared connection | Decompiled vendor client | Documented in `docs/protocol-notes.md` §8; **not implemented** by this collection — see Tier 4 |
| This BMC answers a **failed** login with HTTP 200, not 401/403, with a `SESSION_COOKIE` value carrying a `Failure_Login_*` marker | `nesvet/nojava-ipmi-kvm` PR #40 (MIT) | `plugins/module_utils/asp.py` (`_FAILURE_LOGIN_PREFIX`, `AspClient.login`) |
| Fetching `/Java/jviewer.jnlp` (with `EXTRNIP`/`JNLPSTR` query arguments) is what allocates the KVM/media session server-side, not `getsessiontoken.asp` | `BadCoder1337/rd450x-console` (MIT) and `nesvet/nojava-ipmi-kvm` PR #39 | `plugins/module_utils/asp.py` (`AspClient.allocate_media_session`) |
| `pyghmi.ipmi.command.Command`'s constructor is synchronous and blocking when built without an `onlogon` callback, and every session-establishment failure surfaces uniformly as `pyghmi.exceptions.PyghmiException` | Read directly from `pyghmi`'s installed source (`pyghmi/ipmi/private/session.py`), in a disposable virtualenv with no BMC reachable from it — no network request made to any BMC to establish this | `plugins/module_utils/ipmi.py` (`IpmiClient._connect`) |
| `Command.get_power()` returns only `{'powerstate': 'on'}` or `{'powerstate': 'off'}`; `Command.get_mci()` returns a bare `str` (or `None`), never a dict | Read directly from `pyghmi`'s source, confirmed by a live capture against the target hardware (see Tier 3) | `plugins/module_utils/ipmi.py` |
| `Command.set_power(state, wait=...)` sends the chassis-control command immediately, then — only for a confirmable target state — polls in a bounded loop, raising `IpmiException("System did not accomplish power state change")` specifically when the command was accepted but confirmation timed out | Read directly from `pyghmi`'s source | `plugins/module_utils/ipmi.py` (`_SET_POWER_WAIT_TIMEOUT_MESSAGE`, `IpmiClient.set_power_state`) |
| `Command.set_bootdev()` returns `{'error': ...}` for an unrecognised device name (does not raise), but raises `IpmiException` for an IPMI-level rejection of either of its two underlying raw commands | Read directly from `pyghmi`'s source | `plugins/module_utils/ipmi.py` (`IpmiClient.set_boot_device`) |
| `community.general.ipmi_power`'s documented `state` choices (`on`, `off`, `shutdown`, `reset`, `boot`) and `community.general.ipmi_boot`'s documented `bootdev` choices (`network`, `floppy`, `hd`, `safe`, `optical`, `setup`, `default`) | That module's own published documentation, not invented here | `plugins/module_utils/models.py` (`POWER_STATES`, `BOOT_DEVICES`) |
| The AST2400/ASMB8 generation predates Redfish support, which arrived with AST2500/ASMB9 | Stated hardware-generation fact, not a live probe — no Redfish endpoint is ever contacted by this collection | `plugins/modules/asmb8_info.py` (`build_capabilities`'s `redfish` entry) |
| The IVTP console header is 8 bytes: `type` (u16 LE), `pktSize` (u32 LE), `status` (u16 LE) | `BadCoder1337/rd450x-console`'s documentation for a *different* vendor's AMI MegaRAC board; corroborated for opcode numbering by a local decompilation of this board's own `com.ami.kvm.jviewer.kvmpkts.IVTPPktHdr` | `plugins/module_utils/ivtp.py` (`Header`), `docs/protocol-notes.md` §7 |
| The full IVTP handshake sequence for this board — unsolicited `SESSION_ACCEPTED` (23) greeting, optional `GET_WEB_TOKEN` (21), `VALIDATE_VIDEO_SESSION` (18), `VALIDATE_VIDEO_SESSION_RESPONSE` (19), `RESUME_REDIRECTION` (6) — with **no** `CONNECTION_COMPLETE`/`KEEP_ALIVE` step, unlike the MIT reference implementation's reconnect-aware sequence for a different board | Local decompilation of `com.ami.kvm.jviewer.kvmpkts.IVTPPktHdr`/`KVMClient` for this board's exact firmware; the decompiled client defines no opcode 57/58 at all, and the resulting simpler sequence matches this collection's own live-captured 8-byte greeting exactly | `plugins/module_utils/ivtp.py` (`open_channel`) |
| `VALIDATE_VIDEO_SESSION_RESPONSE`'s status byte vocabulary (`INVALID_SESSION`=0, `VALID_SESSION`=1, `KVM_DISABLED`=2, `INVALID_VIDEO_TOKEN`=3, `INVALID_CDROM_TOKEN`=4, `INVALID_FLOPPY_TOKEN`=5) and `STOP_SESSION_IMMEDIATE`'s reason codes | Decompiled `KVMClient`'s own named int constants | `plugins/module_utils/ivtp.py` (`SESSION_*`, `STOP_*`) |
| `VIDEO_FRAGMENT`'s 2-byte little-endian fragment-number prefix, with bit `0x8000` marking the last fragment and the low 15 bits being `0` marking the first | Decompiled `FragNumReader`/`FragReader`; independently corroborated by the MIT reference implementation's `client.go` read loop, which implements the identical convention for a different board — one of the strongest-corroborated facts in `docs/protocol-notes.md` | `plugins/module_utils/ivtp.py` (`parse_video_fragment`, `FrameReassembler`) |

Every row above is a claim about the **protocol or library**, sourced from
something other than this collection's own code or a live capture. None of
them is, by itself, a claim that this collection's own *implementation* has
been exercised against real firmware — that is Tier 3.

## Tier 2: Unit/mock tested

Exercised by `tests/unit/` against deterministic fixtures — `AspMockServer`
(`tests/integration/mock_servers/asp_server.py`) and `IusbMockServer`
(`tests/integration/mock_servers/iusb_server.py`) — never against the real
board.

| Claim | Where tested | Where used |
|---|---|---|
| `Header.marshal`/`Header.parse` round-trip byte-exactly, including the checksum computation | `tests/unit/plugins/module_utils/test_iusb.py` | `plugins/module_utils/iusb.py` |
| `CDROMDevice` answers `TEST UNIT READY`, `READ CAPACITY(10)`, `READ(10)`, `READ(12)`, and `READ TOC` against golden byte vectors, including big-endian LBA/length decoding | `tests/unit/plugins/module_utils/test_iusb.py` | `plugins/module_utils/iusb.py` |
| `Session.serve_forever` never raises on an idle socket at a frame boundary (`IdleTimeout` is swallowed, not propagated), while a mid-frame stall raises `ConnectionError_` | `tests/unit/plugins/module_utils/test_iusb.py` | `plugins/module_utils/iusb.py` |
| `Cache` stays bounded to `DEFAULT_MAX_WINDOWS` windows regardless of backing-image size (verified at both 200 MiB and 1.5 GiB sparse-file scale) | `tests/unit/plugins/module_utils/test_iusb.py` | `plugins/module_utils/iusb.py` |
| `AspClient.login()` detects the `Failure_Login_*` marker and raises `AuthenticationError` even though the HTTP status is 200 | `tests/unit/plugins/module_utils/test_asp.py`, `tests/unit/mock_servers/test_asp_server.py` | `plugins/module_utils/asp.py` |
| `AspClient` treats a completed-connection, never-answered request (`hang_before_response`) as retryable up to `max_retries`, then raises `BmcBusyError` with `indeterminate=True` | `tests/unit/plugins/module_utils/test_asp.py` | `plugins/module_utils/asp.py` |
| `parse_jnlp_arguments` tolerates an unescaped `&` inside an `<argument>` value that would fail strict XML parsing | `tests/unit/plugins/module_utils/test_asp.py`, `tests/unit/mock_servers/test_asp_server.py` | `plugins/module_utils/asp.py` |
| `JnlpSession.from_arguments` derives `port_mode` (`single_port` / `dedicated_ports` / `unknown`) correctly from which arguments are present | `tests/unit/plugins/module_utils/test_models.py` | `plugins/module_utils/models.py` |
| `errors.redact()` strips `Authorization`/`Cookie` headers, `WEBVAR_PASSWORD`, `SESSION_COOKIE`/`kvmtoken`/`webcookie` values, and generic `password=`/`token=`-shaped text, in both quoted and bare forms | `tests/unit/plugins/module_utils/test_errors.py` | `plugins/module_utils/errors.py` |
| `IpmiClient` classifies a `pyghmi` session-establishment failure into `AuthenticationError`/`TimeoutError_`/`ConnectionError_` from `errormsg` text alone | `tests/unit/plugins/module_utils/test_ipmi.py` | `plugins/module_utils/ipmi.py` |
| `asmb8_power`'s convergence check (`on`/`off` compare against `get_power_state()`; `shutdown`/`reset`/`boot` always issue) and its check-mode behaviour | `tests/unit/plugins/modules/test_asmb8_power.py` | `plugins/modules/asmb8_power.py` |
| `asmb8_boot` refuses `persistent=true` before opening any IPMI session, and its idempotency compare against `get_bootdev()`'s possibly-absent `uefimode` key | `tests/unit/plugins/modules/test_asmb8_boot.py` | `plugins/modules/asmb8_boot.py` |
| `asmb8_info` degrades per-field on an IPMI read failure rather than failing the whole module, and never fetches the JNLP (so `media.port_mode` is always `"unknown"`) | `tests/unit/plugins/modules/test_asmb8_info.py` | `plugins/modules/asmb8_info.py` |
| `asmb8_media`'s always-run reclamation pass finds and stops every *other* locally-tracked session against the same endpoint before a fresh attach, and never signals anything in check mode | `tests/unit/plugins/module_utils/test_media_session.py`, `tests/unit/plugins/modules/test_asmb8_media.py` | `plugins/module_utils/media_session.py` |
| `asmb8_media`'s attach never reports success on anything short of the daemon reaching `STATE_ATTACHED`, and classifies an unconfirmed-but-still-running daemon as an `indeterminate` timeout rather than a plain failure | `tests/unit/plugins/modules/test_asmb8_media.py` | `plugins/modules/asmb8_media.py` |
| `OperationReceipt.to_dict()` runs every string value through `errors.redact()` as a backstop | `tests/unit/plugins/module_utils/test_models.py` | `plugins/module_utils/models.py` |

None of the above touches a real socket to a real BMC. See
[`docs/testing.md`](testing.md) for how to run this tier and what the mock
servers' own docstrings mark as `VERIFIED LIVE` versus `ASSUMED, NOT
VERIFIED`/`UNCONFIRMED` in their *own* default behaviour — a mock faithfully
reproducing an assumption is not the same claim as this tier's rows above,
which are about this collection's client code, not the mock's fidelity.

## Tier 3: Verified against real firmware

Every row cites
[`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md),
observed against the one target board (ASUS Z10PE-D16 WS / ASMB8-iKVM,
firmware 1.14/1.14.2) on 2026-08-08.

### Transport and trust

| Claim | Evidence |
|---|---|
| TLS 1.2 only; TLS 1.0/1.1/1.3 all refused at the handshake | "Transport and trust" |
| Exactly one ciphersuite offered, `AES256-GCM-SHA384` (static RSA, no forward secrecy) | "Transport and trust" |
| The factory certificate is self-signed and already expired (validity 2016-06-01 to 2026-05-30); chain validation can never succeed against it | "Transport and trust" |
| The BMC's own clock is wrong (reported `2018-01-25` while actually running in 2026) | "Transport and trust" |
| The web server is HTTP/1.0, no keep-alive, caps at 20 sessions, and keeps separate worker pools per listener; concurrent load exhausted the port-80 pool while port 443 stayed responsive | "Transport and trust" |

### Authentication and session allocation

| Claim | Evidence |
|---|---|
| `POST /rpc/WEBSES/create.asp` returns a 35-character `SESSION_COOKIE` on success, reused as `Cookie: SessionCookie=<value>` | "Authentication and session allocation" |
| On bad credentials this BMC returns HTTP 200 with a `Failure_Login_*` marker, not a non-2xx status | "Authentication and session allocation" |
| `GET /rpc/getsessiontoken.asp` returns an empty `STOKEN` — not a usable token source | "Authentication and session allocation" |
| Fetching `/Java/jviewer.jnlp` is what actually allocates the session and mints a usable 16-character `-kvmtoken`; the returned `-webcookie` is byte-identical to the session cookie | "Authentication and session allocation" |
| `-kvmsecure`/`-vmsecure`/`-kvmport` follow the scheme used to *fetch* the JNLP, not a persistent board setting (observed `-kvmport 80 -kvmsecure 0 -vmsecure 0` over HTTP, `-kvmport 443 -kvmsecure 1 -vmsecure 1` over HTTPS minutes later on the same board) | "Authentication and session allocation" |

### Port behaviour

| Claim | Evidence |
|---|---|
| Ports 5120/5122/5123/7578 return TCP RST before a session is allocated, and bind immediately after a JNLP fetch | "Port behaviour" |
| A closed `cd_port` before any attach is normal, not an error | "Port behaviour" |
| Dedicated ports bind even when the JNLP reports `-singleportenabled 1` | "Port behaviour" |

### iUSB virtual media

| Claim | Evidence |
|---|---|
| Auth request/ACK byte layout, including the ACK's zeroed major/minor/headerLen | "iUSB virtual media — verified working" |
| `deviceType` `0x85` observed on the server's own request frames (`0x80 \| 0x05`) | "iUSB virtual media — verified working" |
| Header reserved bytes (offset 28) are not always zero on real server frames (`bc 38 02 bf` observed) | "iUSB virtual media — verified working" |
| `cmdctr` (SCSI envelope offset 4) is non-sequential (observed 3, 7, 16) | "iUSB virtual media — verified working" |
| `READ CAPACITY(10)` response verified byte-exact against a 1052-sector test image | "iUSB virtual media — verified working" |
| LBA and transfer-length fields inside the SCSI CDB are big-endian inside the little-endian iUSB wrapper (proven via LBA 16, 17, 18 requests, not the byte-swapped 268,435,456) | "iUSB virtual media — verified working" |
| The media session survives a host reset, staying authenticated across a power cycle | "iUSB virtual media — verified working" |
| Control opcode `0xF3` observed from the BMC during normal operation (previously undocumented) | "iUSB virtual media — verified working" |

### Boot chain and streaming performance

| Claim | Evidence |
|---|---|
| The real firmware's El Torito boot chain (LBA 0, 1, 16, 17, boot catalog at 4660, terminator at 18, BIOS image at 6025, UEFI image at 156), matching `xorriso -report_el_torito` on the same image exactly | "Boot chain, proven" |
| The bootloader shifts to multi-block reads of up to 16 blocks (32 KiB) once past single-sector probing | "Boot chain, proven" |
| Throughput ≈ 800–900 KB/s with 16-block reads | "Measured performance and behaviour" |
| A healthy attached session can be completely idle for at least 130 consecutive seconds with no failure | "Measured performance and behaviour" |
| A correct read at LBA 832,880 of an 833,095-sector image rules out 16-bit LBA truncation | "Measured performance and behaviour" |
| One run served 2,839 read requests / 33,038 sectors (~71.0 MiB) sustained | "Measured performance and behaviour" |

### The stock-ISO menu trap

| Claim | Evidence |
|---|---|
| A stock Proxmox VE ISO stops at its GRUB menu and waits forever, because `timeout`/the `Automated` entry exist only inside an `if [ -f auto-installer-mode.toml ]` conditional a stock image lacks | "The stock-ISO menu trap" |
| Rebuilding the ISO with `xorriso -boot_image any replay` and an unconditional timeout produces an image that boots without human input (boot LBAs shift slightly: catalog 4660→4668, BIOS 6025→6029, UEFI 156→164) | "Workaround proven on hardware" |

### Console

| Claim | Evidence |
|---|---|
| IPMI Serial-over-LAN opens successfully via `pyghmi` but delivers zero data, because BIOS console redirection is not enabled on COM1 | "Console" |
| Plaintext port 7578 (KVM) greets with exactly 8 bytes (`17 00 00 00 00 00 00 00`), matching the documented 8-byte IVTP header (`type=0x0017`, `pktSize=0`, `status=0`) | "Console" |

### IPMI

| Claim | Evidence |
|---|---|
| `pyghmi` establishes an IPMI 2.0 session over UDP 623 with the default cipher suite, no special flags | "IPMI" |
| `get_power()` returned `on`; `get_bootdev()` returned `{'bootdev': 'default', 'persistent': False, 'uefimode': False}` | "IPMI" |
| `set_bootdev('cd', persist=False)` set the override, and it reverted to `default` after the following reset — confirming true one-time semantics | "IPMI" |
| `get_mci()` returns a bare string, not a dict | "IPMI" |

## Tier 4: Still unproven

Named specifically, per
[`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md)'s
own "Still unproven — do not claim these" section, plus a few this
documentation pass adds from reading the code against that evidence.

- **A completed, unattended OS install, start to finish.** The furthest this
  collection has reached is the installer streaming its squashfs. No install
  has been driven to completion, on any module or the `asmb8_baremetal_install`
  role.
- **Whether the guest OS can obtain its own media session once booted.**
  Linux re-enumerates USB storage with its own driver, and `cd-media` allows
  exactly one session with no server-side reclaim timeout. If this
  collection's own daemon is still holding the slot when that happens, the
  guest may be unable to reach its own media — a failure mode that would
  surface *after* the installer appears to start. Untested.
- **KVM video decoding.** The IVTP greeting and its 8-byte framing are
  understood (§7 of `docs/protocol-notes.md`); no console frame has ever been
  decoded from this board, and no code in this collection attempts it.
- **The virtual floppy and virtual hard-disk device classes.** Their ports
  (5122, 5123) bind, but only the CD-ROM class (port 5120) has ever been
  exercised, live or in this collection's own mock.
- **Any board other than the one target machine.** One machine, one firmware
  version (1.14/1.14.2). This is repeatability at best, not a compatibility
  guarantee — including whether this collection would even successfully
  authenticate against a different ASMB8 unit, let alone a different AMI
  MegaRAC-based board.
- **The exact numeric meaning of ACK `connectionStatus` values `5` and `8`
  ("device in use") for this specific board.** These values are ported from
  the decompiled JViewer sources (Tier 1), but no live capture has
  independently confirmed either value against a real "device already in use"
  rejection from this board — see `docs/protocol-notes.md` §3.
- **Which `loej` byte value(s) a real eject request actually sends on this
  board.** The exact-equality-to-`2` *comparison* is Tier 1 (decompiled vendor
  client); whether a live eject actually produces `0x02` has never been
  captured — see `docs/protocol-notes.md` §5.
- **Whether an AMI control opcode `≥ 0xF0` other than `0xF1`/`0xF2`/`0xF3`/`0xF6`
  can reach `CDROMDevice.handle` in practice.** `plugins/module_utils/iusb.py`
  treats this as believed-unreachable per the decompiled dispatcher, and flags
  it explicitly as a live-hardware verification target rather than a confirmed
  fact.
- **Single-port mode (HTTP CONNECT tunnelling).** Its *design* is Tier 1
  (decompiled vendor client), but this collection has never implemented or
  exercised it — the target board's current configuration exposes dedicated
  per-device ports, and no code path in this collection speaks single-port
  mode at all.
- **`asmb8_console` and `plugins/module_utils/ivtp.py` have zero live-hardware
  evidence and zero unit/mock test coverage of the handshake state machine —
  the only module and `module_utils` file in this collection with neither.**
  (This module was named `asmb8_redirection` until it was split from the
  now-separate, differently-behaved module of that name -- see
  `changelogs/fragments/` and `docs/asmb8_redirection.md`; nothing about its
  own evidence changed in that split.) Every protocol fact it relies on is
  Tier 1 (decompiled vendor client analysis) with nothing behind it at Tier 2
  or Tier 3. Specifically unverified:
  - Whether the `VALIDATE_VIDEO_SESSION` packet's `pktSize` header field
    should be 324 (this collection's choice, self-consistent with every other
    packet-building method in the decompiled client) or 332 (what the
    decompiled client's own method for *this specific packet* inconsistently
    writes). See `plugins/module_utils/ivtp.py`'s module docstring,
    disagreement 2.
  - Whether `GET_WEB_TOKEN` (opcode 21) is actually *required* by the BMC, or
    merely tolerated — `send_get_web_token` defaults to `true` on a guess
    about which decompiled code path this headless client is closest to, not
    on a confirmed requirement.
  - Whether `client_username`'s value has any effect on BMC behaviour at all.
  - Whether a real "device already in use" / session-slot-exhaustion
    rejection from the KVM service looks anything like what
    `plugins/module_utils/ivtp.py` assumes — no such rejection has been
    observed.
  - **`asmb8_console.py`'s own `DOCUMENTATION` claims the KVM service
    permits 4 concurrent sessions with an 1800-second server-side inactivity
    timeout, attributed only to "the task brief this collection was built
    against."** This is not sourced from the decompiled vendor client, a live
    capture, or any other authoritative reference this document cites
    elsewhere, and does not belong at Tier 1 or Tier 3 despite reading like a
    confident, specific fact. Treat both numbers as unverified until a real
    source backs them. The new, separate `asmb8_redirection` module's own
    static catalog repeats these same two figures for the same seven
    services, with the same caveat -- see the next item.
  - **`asmb8_redirection`'s `known`/`enabled` signals are a static catalog,
    not a live query.** Its `reachable` signal is a genuine, live, per-run TCP
    probe -- that part is Tier 1 in the ordinary sense (a bare socket connect,
    nothing protocol-specific to source). But `known` and `enabled` are read
    from a catalog built into the module from the BMC's own Services page as
    read from its web UI, per `docs/hardware-evidence-2026-08-08.md`'s
    "Service capacities, and a provenance caveat" section -- not observed on
    the wire, and not re-queried live on any run, because no sourced RPC
    exists for fetching that page's state over the wire. Treat `enabled` as
    "what the vendor's Services page showed once," not "what is true right
    now."
  - **Whether any RPC exists on this BMC's `.asp` surface for toggling a
    service's enablement.** Investigated specifically for `asmb8_redirection`
    and not found; `plugins/module_utils/asp.py` documents every RPC this
    collection has sourced, and none of them toggle a service. `state` is
    accepted by the module's argument spec (so it fails with a clear,
    specific message rather than an "unrecognised parameter" error) but
    always fails with `error_class=unsupported_capability`.
- **`decoded_frame` capture is a deliberate non-goal, not a proven gap.**
  `asmb8_console` refuses to decode AMI/ASPEED video into pixels
  (`error_class=unsupported_capability`) rather than approximating one. This
  is correct, honest behaviour — listed here only so a reader does not read
  "video decoding is unproven" (above) as "video decoding was attempted and
  failed."
- **Whether `asmb8_media`'s `.asp`/JNLP-mediated session token itself carries
  any confidentiality or authentication guarantee independent of the
  underlying web session.** Not measured either way — see
  [SECURITY.md](../SECURITY.md).
- **Hardware-in-the-loop CI.** `.circleci/config.yml`'s `hardware` workflow
  (observe → login → media-attach → boot-once → reset → kvm, each behind its
  own approval gate) is fully wired, and every hardware playbook it invokes
  (`tests/hardware/*.yml`) now exists and is `ansible-playbook --syntax-check`
  and `ansible-lint` clean — see `tests/hardware/README.md`. **It has never
  actually run against real hardware.** Building the playbooks is necessary
  for this pipeline to ever produce evidence, but it is not itself evidence:
  everything in Tier 3 above still came from a manual, one-off session
  against the lab board, not from this pipeline. A future run of this
  workflow is what would move `asmb8_console`/`ivtp.py` in particular (see
  below) out of "zero live-hardware evidence".
