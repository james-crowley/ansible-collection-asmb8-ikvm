# Hardware evidence log — ASUS Z10PE-D16 WS / ASMB8-iKVM, 2026-08-08

Every claim here was observed directly against real hardware on the date above.
Nothing in this file is inferred from a datasheet, a reference implementation, or
another vendor's board. It exists so that `docs/capability-matrix.md` can cite
falsifiable evidence rather than assertions, and so a future maintainer can tell
what was actually tested from what was merely believed.

## Target

| Property | Value |
|---|---|
| Motherboard | ASUS Z10PE-D16 WS |
| BMC | ASMB8-iKVM add-in card, ASPEED AST2400 |
| BMC firmware | 1.14, auxiliary 1.14.2 |
| Redfish | absent — generational, not a firmware gap (Redfish arrived with AST2500/ASMB9) |

## Transport and trust

- **TLS 1.2 only.** TLS 1.0, 1.1 and 1.3 are all refused at the handshake.
- **Exactly one ciphersuite: `AES256-GCM-SHA384`** — static RSA key exchange, no
  forward secrecy. Modern OpenSSL/Python exclude non-PFS suites from their
  default cipher list, so an out-of-the-box `requests` call fails with
  `SSLV3_ALERT_HANDSHAKE_FAILURE`. `curl` is more permissive and succeeds against
  the same endpoint, so "curl works but Python does not" is expected here and is
  not a bug in this collection. See `BMC_CIPHERS` in
  `plugins/module_utils/asp.py`.
- **Certificate is self-signed AND expired.** Subject and issuer are both
  `C=US, O=American Megatrends Inc, OU=Service Processors, CN=AMI`; validity
  2016-06-01 to **2026-05-30**. CA-chain validation therefore cannot succeed on
  this board, ever. Fingerprint pinning or an explicit insecure acknowledgement
  are the only workable trust policies.
- **The BMC's clock is wrong.** It reported `Thu Jan 25 17:40:30 2018`. Never
  rely on a BMC-supplied timestamp, and note that from the BMC's own point of
  view its expired certificate is still valid.
- **The web server is HTTP/1.0 with no keep-alive**, caps at 20 sessions, and
  keeps **separate worker pools per listener**. Concurrent requests exhausted the
  port-80 pool: the BMC continued completing TCP handshakes while never serving a
  request, and stayed that way for a long time — while port 443 remained
  perfectly responsive throughout. This is the observation behind
  `ErrorClass.BMC_BUSY` and behind this collection serialising all BMC HTTP
  access. HTTPS is the supported transport.

## Authentication and session allocation

- `POST /rpc/WEBSES/create.asp` with `WEBVAR_USERNAME` / `WEBVAR_PASSWORD`
  returns HTTP 200 with `'SESSION_COOKIE':'<35-char value>'`, used thereafter as
  `Cookie: SessionCookie=<value>`.
- **On bad credentials this BMC returns HTTP 200**, not 401, with a
  `SESSION_COOKIE` value containing a `Failure_Login_*` marker. A client that
  checks only the status code will treat a rejected login as successful. See
  `nesvet/nojava-ipmi-kvm` PR #40 and the guard in `asp.py`.
- `GET /rpc/getsessiontoken.asp` returned an **empty** `STOKEN`. This endpoint is
  not a usable source of a media/KVM token.
- `GET /Java/jviewer.jnlp?EXTRNIP=<ip>&JNLPSTR=JViewer` is what actually
  **allocates the session** and mints a usable 16-character `-kvmtoken`. The
  returned `-webcookie` is byte-identical to the `SESSION_COOKIE`, so it is not a
  separate secret.
- **The JNLP's `-kvmsecure` / `-vmsecure` / `-kvmport` values follow the scheme
  used to fetch it**, not a persistent board setting. Fetched over HTTP:
  `-kvmport 80`, `-kvmsecure 0`, `-vmsecure 0`. Fetched over HTTPS minutes later:
  `-kvmport 443`, `-kvmsecure 1`, `-vmsecure 1`.

## Port behaviour

**The media and KVM listeners are on-demand.** Before a JNLP fetch allocates a
session, ports 5120/5122/5123/7578 return TCP **RST** — reachable host, nothing
listening. Immediately after a JNLP fetch they are all bound. **A closed port
5120 is not an error condition**; it means no session is allocated yet.

Observed with encryption disabled in the BMC's config:

| Port | Service | State after session allocation |
|---|---|---|
| 5120 | cd-media (plaintext) | open |
| 5122 | fd-media (plaintext) | open |
| 5123 | hd-media (plaintext) | open |
| 7578 | kvm (plaintext) | open |
| 5124 | cd-media (secure) | refused |
| 7582 | kvm (secure) | refused |

The dedicated ports bind **even when the JNLP reports
`-singleportenabled 1`** — that argument tells the vendor's Java client to prefer
single-port mode; it does not stop the BMC binding dedicated listeners. This is
why this collection needs no HTTP CONNECT tunnelling to reach virtual media.

## iUSB virtual media — verified working

Authentication, then a full CD-ROM emulation exchange, all with no Java anywhere
in the path:

- Auth request: 32-byte header + 128-byte payload, opcode `0xF2` at payload
  offset 9, 16-character token at payload offset 31.
- Auth ACK: opcode `0xF1` at payload offset 9, **status `0x01` at payload offset
  30**. Note the ACK's header returned major/minor/packetHeaderLen as **zero**
  rather than 1/0/32.
- The server sets `deviceType = 0x85` on its request frames, OR-ing `0x80` into
  our `0x05`.
- Header reserved bytes at offset 28 are **not always zero** — observed
  `bc 38 02 bf`.
- `cmdctr` at payload offset 4 is **non-sequential** — observed 3, 7, 16. Key off
  the header sequence number, which the client echoes verbatim.
- `READ CAPACITY(10)` response verified byte-exact against a 1052-sector test
  image: `dataPacketLen` 37 (= 29 + 8), response length `0x08` at payload offset
  25, then last-LBA `00 00 04 1b` (1051) and block size `00 00 08 00` (2048),
  both big-endian.
- **LBA and transfer-length fields inside the SCSI CDB are big-endian, inside a
  little-endian iUSB wrapper.** Proven by the firmware requesting LBA 16, 17, 18
  — a byte-swapped implementation would have asked for 268435456.
- **The media session survives a host reset.** It stayed authenticated across a
  power cycle and kept serving.
- Observed control opcodes from the BMC during normal operation included `0xF1`
  and **`0xF3`** (the latter previously undocumented in our analysis).

### Boot chain, proven

Serving a real Proxmox VE 9.2-1 ISO, the firmware read, in order: LBA 0 and 1,
LBA 16 (ISO9660 Primary Volume Descriptor), LBA 17 (El Torito Boot Record
Descriptor), **LBA 4660 — the El Torito boot catalog**, LBA 18 (terminator), then
LBA 6025 (the BIOS boot image) and LBA 156 (the UEFI boot image).

Those three unusual LBAs match `xorriso -report_el_torito` on the same image
exactly (catalog 4660, BIOS image at 6025, UEFI image at 156), which independently
confirms the client served correct bytes and not merely plausible ones.

The bootloader then took over, shifting from single-sector probing to multi-block
reads of up to 16 blocks (32 KiB) with irregular sizes at extent boundaries —
i.e. a real filesystem driver walking directory records.

### Measured performance and behaviour

- **Throughput ≈ 800–900 KB/s** with 16-block reads. `pve-installer.squashfs`
  alone is **614 MB**, so a real install streams for **13+ minutes**. Timeouts
  throughout this collection are sized for that. **This must be measured over
  the bulk streaming phase only** — exclude the ~2-minute bootloader-menu idle
  at the start of a run and the multi-second installer-side pauses later in it.
  Correctly windowed: 60.0 MiB over 78 s from 2.0m→3.3m elapsed ≈ **788 KB/s**;
  a single 15 s interval, 11.6 MiB from 2.5m→2.8m ≈ **660 KB/s**. Dividing
  *total* bytes by *total* elapsed time instead — 119.4 MiB over 270 s for the
  same run — gives ≈453 KB/s, roughly half the real streaming rate, and that
  single averaging error has already caused this accurate figure to be
  "corrected" wrongly more than once. Always quote a rate with the window it
  was measured over on this media path; never total-over-total.
- **A healthy attached session can be completely idle for a very long time.** We
  measured **130 consecutive seconds** of zero requests while the host sat at a
  bootloader menu, after which reads resumed normally. There is no safe upper
  bound on idle: a host can sit at an installer prompt or firmware setup screen
  indefinitely. Idle must never be treated as failure. This is why
  `tests/unit/mock_servers/test_iusb_server.py` pins idle-tolerance explicitly.
- **High LBAs are served correctly.** Observed a correct read at **LBA 832,880**
  of an 833,095-sector image. A 16-bit truncation bug would have silently served
  sector 51,824 instead; streaming continued correctly, so no truncation exists.
- One complete run served **2,839 read requests / 33,038 sectors (~71.0 MiB)**, and
  climbing, sustained.

## The stock-ISO menu trap

**A stock Proxmox VE ISO stops at its GRUB menu and waits forever.** Confirmed
both from the read trace (loading halted after ~2.8 MB) and visually by the
direct observation of the console. The cause is in the ISO's own `grub.cfg`:

```
if [ -f auto-installer-mode.toml ]; then
    set timeout-style=menu
    set timeout=10
    menuentry 'Install Proxmox VE (Automated)' ... proxmox-start-auto-installer
fi
```

`timeout` — and the `Automated` entry — exist **only** when
`auto-installer-mode.toml` is present at the ISO root, which a stock image lacks.
There is therefore no timeout at all. The ISO root does carry a zero-byte
`auto-installer-capable` marker, so the image supports auto-install once prepared
by `proxmox-auto-install-assistant prepare-iso`.

**Booting an unprepared installer ISO does not produce an unattended install.**

### Workaround proven on hardware

Rebuilding the ISO with an unconditional timeout and an explicit default entry
makes it proceed without human input. Verified with `xorriso` in a
`debian:bookworm-slim` container:

```
xorriso -indev in.iso -outdev out.iso \
        -boot_image any replay -compliance no_emul_toc \
        -map /local/grub.cfg /boot/grub/grub.cfg -commit
```

`-boot_image any replay` preserves the hybrid El Torito structure (both BIOS and
UEFI images, and the `MBR protective-msdos-label grub2-mbr GPT APM` layout). The
rebuilt image booted correctly; only the boot LBAs shifted slightly (catalog
4660 → 4668, BIOS image 6025 → 6029, UEFI image 156 → 164).

## Console

- **IPMI Serial-over-LAN opens successfully** on this board — a SOL session
  established without error via `pyghmi` — but delivered **zero data**, because
  the BIOS does not have console redirection enabled on COM1. The Proxmox ISO's
  GRUB does attempt `serial --unit=0 --speed=115200` and appends serial to
  `terminal_input`/`terminal_output`, and the ISO ships an
  `Install Proxmox VE (Terminal UI, Serial Console)` entry using
  `console=ttyS0,115200` — so SOL becomes useful once BIOS redirection is turned
  on, which itself needs console access. Chicken and egg.
- **The KVM channel speaks IVTP and the server greets first.** Connecting to
  plaintext 7578 yielded exactly 8 bytes:
  ```
  17 00 00 00 00 00 00 00
  ```
  Parsed against the documented 8-byte little-endian IVTP header (`type` u16,
  `pktSize` u32, `status` u16) that is `type=0x0017`, `pktSize=0`, `status=0` — a
  well-formed greeting matching an independent clean-room implementation written
  against a *different vendor's* AMI MegaRAC board. Strong evidence the protocol
  is genuinely shared rather than coincidentally similar.

## Service capacities, and a provenance caveat

The BMC's own Services page (read from its web UI, not observed on the wire by
this collection) reports:

| Service | State | Nonsecure | Secure | Timeout (s) | Max sessions |
|---|---|---|---|---|---|
| web | Active | 80 | 443 | 1800 | 20 |
| kvm | Active | 7578 | 7582 | 1800 | 4 |
| cd-media | Active | 5120 | 5124 | N/A | 1 |
| fd-media | Active | 5122 | 5126 | N/A | 1 |
| hd-media | Active | 5123 | 5127 | N/A | 1 |
| ssh | Active | N/A | 22 | 600 | N/A |
| telnet | **Inactive** | 23 | N/A | 600 | N/A |

**Read this table with care.** It is the BMC's self-report of *configured* service
state, and this collection independently confirmed only part of it:

- **Confirmed on the wire:** the port numbers, the plaintext-versus-secure split
  (only the nonsecure ports bound, with encryption disabled in config), and that
  the listeners are on-demand.
- **NOT independently confirmed:** the session counts and the timeout values.
  In particular, `asmb8_console`'s documentation (this table's figures were
  originally cited from `asmb8_redirection` before that module was split in
  two -- see `changelogs/fragments/` -- into a session-opening module,
  renamed `asmb8_console`, and a service-enablement-reporting module that
  kept the `asmb8_redirection` name and now also cites this same table)
  states that the KVM service allows 4 concurrent sessions with an
  1800-second inactivity timeout, and
  `asmb8_media`'s reasoning leans on cd-media allowing exactly one session with
  no timeout. Those figures come from **this table only**. We never opened five
  KVM sessions to see the fifth rejected, and never left a session idle for
  1800 seconds to watch it be reclaimed.

The cd-media "1 session, no timeout" figure is the one that matters most, because
this collection's eject-before-insert reclamation logic exists because of it. It
is *consistent* with what we observed — a media session survived a host reset and
was still bound long afterward with nothing holding it — but "consistent with" is
weaker than "measured." Treat the single-slot behaviour as well-supported and the
exact numeric limits as vendor self-report.

## Serial-over-LAN: configured correctly and still silent

This is recorded because it cost real time and the negative result is worth
knowing: **on this board, an IPMI SOL session can establish cleanly, be fully
configured, and still deliver no console output at all.**

What was verified over IPMI (`Get/Set SOL Configuration Parameters`, netfn `0x0c`
commands `0x22`/`0x21`, and `Get/Set User Payload Access`, netfn `0x06` commands
`0x4D`/`0x4C`):

- A SOL session opens without error via `pyghmi`'s console API.
- The channel-level SOL payload was found **disabled** (parameter 0) and was
  enabled successfully. This is a BMC-side setting entirely separate from the
  platform firmware's own console-redirection options — enabling redirection in
  firmware setup is **not** sufficient on its own.
- SOL payload access was already granted for the administrative user.
- Both plausible bitrates were tried (parameters 5 and 6), matching each of the
  two speeds the firmware setup exposes.

With all three of those satisfied, **zero bytes arrived** across repeated host
resets, through the whole power-on self-test window. Not garbled output — none.

Leading hypotheses, none confirmed, all requiring physical firmware-setup access
rather than anything reachable over IPMI:

- The firmware's console-redirection master toggle for the port in question may
  be off even when its parameters (speed, terminal type, data bits) are set.
- The BMC's SOL may be wired to a different serial port than the one firmware is
  redirecting to.
- If the port the BMC uses is configured for **hardware RTS/CTS flow control**,
  SOL would stall waiting on a signal no BMC-side virtual cable asserts.
- This is 2016-era AMI firmware with a documented defect history; a partial or
  broken SOL implementation would not be surprising.

**Practical consequence for this collection:** do not depend on SOL for
observing an installer. Everything in this collection was developed and verified
without it, by correlating the media channel's own read pattern — which sectors
the firmware asks for, and when — against the ISO's structure. That technique is
described under "Boot chain, proven" above and turned out to be sufficient to
diagnose a bootloader stall precisely.

Any SOL configuration this investigation changed was restored to its original
state afterwards.

## IPMI

Works over UDP 623 with the default cipher suite via `pyghmi`, no special flags.
`get_power()` returned `on`; `get_bootdev()` returned
`{'bootdev': 'default', 'persistent': False, 'uefimode': False}`.
`set_bootdev('cd', persist=False)` set `bootdev` to `optical`, and after the
subsequent reset it **reverted to `default`** — confirming one-time override
semantics and that no persistent boot-order change occurred. Note `get_mci()`
returns a string, not a mapping.

## READ TOC must honour the CDB allocation length

Added 2026-08-09. This was the single most consequential protocol finding of the
whole effort, it took four failed install attempts and five wrong diagnoses to
reach, and it is worth reading in full before touching the SCSI layer.

**The bug.** `CDROMDevice._read_toc` ignored the CDB entirely and always returned
its full 20-byte response — a 4-byte header plus two 8-byte track descriptors.
A Linux initrd probing the emulated drive issued `TEST UNIT READY` followed by
`READ TOC` with an allocation length of **12** bytes (`CDB[7:9] == 0x000c`).
Returning more data than the initiator allocated for is a SCSI protocol violation.

**The consequence, in order.** Linux's optical layer read the oversized response,
concluded the disc had no valid track structure, and therefore never read the
ISO9660 superblock. `blkid` consequently reported no filesystem type. The Proxmox
installer, searching for a device containing its ISO, found no candidate with a
valid `iso9660` signature, reported `no device with valid ISO found, please check
your installation medium`, and dropped to a debug shell.

**Why it hid for so long, and this is the important part.** *Bootloaders never
issue `READ TOC`.* GRUB reads through firmware I/O (the BIOS enumerates the device
as `AMI Virtual CDROM0`), so it loaded the kernel and initrd flawlessly on every
single attempt. Only a real operating system asks for a TOC. The failure therefore
always occurred at exactly the firmware-to-OS handoff, which produced two
misleading effects:

- Every attempt stopped after streaming an identical ~71 MiB / ~2,838 read
  requests — that being precisely kernel + initrd loaded *via GRUB*.
- Four completely different configurations produced byte-identical read traces,
  which invited the inference that the difference between them was irrelevant.
  The correct inference was that execution never reached the point where any of
  them mattered.

**What was wrongly blamed, in order:** answer-file placement; the disk target;
the `auto-installer-mode.toml` `mode` value; a missing `usb-storage` driver; and,
underlying all of them, treating "the evidence is consistent with my hypothesis"
as "the evidence confirms my hypothesis." None of those four was ever reached by
execution.

**What actually identified it.** Two cheap observations, neither of them inference:

1. Instrumenting the client to log *every* inbound opcode, not just reads. That
   showed `TEST UNIT READY` and `READ TOC` arriving ~10 seconds *after* GRUB
   finished — proving a second, later consumer (Linux) was probing the device, and
   killing the missing-driver theory. It also showed `REQUEST SENSE` was **never**
   sent, so the host was not reporting an error: it believed our malformed data.
2. Reading the console. `/dev/sr0` existed, `/proc/partitions` showed it at
   exactly the ISO's size (1,667,264 KiB), and `skipped-devs.txt` showed the
   installer correctly skipping the data disks while *not* skipping `sr0` — i.e.
   it examined our device and rejected it.

**Provenance note.** The defect was inherited from the MIT-licensed Go reference
implementation named in `NOTICE`, whose own `readTOC` also takes the CDB and
ignores it. That project's virtual media is described as live-verified, which it
presumably was — against a bootloader, which would never have exposed this. This
is a genuine divergence from the reference on a real defect, not a stylistic
choice, and `NOTICE` records it as such.

Per SCSI, the two-byte TOC data-length field still reports the **full** available
length even when the returned data is truncated, so an initiator that
under-allocated can detect this and retry with a larger buffer. Implemented that
way, with a regression test asserting the exact 12-byte case plus at-length,
above-length and tiny-allocation behaviour. The pre-existing golden vector could
not have caught this: its allocation length is 0, which correctly means "no limit
stated" and returns everything.

**No other opcode is affected.** `READ CAPACITY(10)` returns exactly the 8 bytes
its requests ask for, and `READ(10)`/`READ(12)` are bounded by their own block
counts.

### Confirmed on hardware, 2026-08-09

The fix above was unit-tested at first landing but not yet observed to fix an
actual install. It has been now: a run made during the `TCP_NODELAY` A/B test
below passed the exact ~71 MiB / 2,839-read point that stopped all four earlier
attempts dead, and continued streaming past 119 MiB with no anomalous opcodes.
The specific failure this fix targeted did not recur. The install itself has
not yet run to completion — see "Still unproven" below — so this confirms the
fix, not a finished install.

## `TCP_NODELAY` on the media socket — a negative result, and a corrected latency figure

Added 2026-08-09, same session as the fix above. `SocketTransport.__init__` now
sets `TCP_NODELAY` on the iUSB media socket (`plugins/module_utils/iusb.py`,
`_disable_nagle`), because iUSB is strictly synchronous request/response — the
traffic shape Nagle's algorithm handles worst — and Python's
`socket.create_connection` leaves Nagle enabled by default. The classic
two-write mistake Nagle is usually blamed for was never present here (each
reply is a single `sendall`); the remaining exposure was Nagle withholding the
final partial segment of a reply pending the peer's delayed ACK.

**The first latency figure quoted for this fix was wrong, and this corrects
it.** One full install had served 16,451 `READ10` requests over roughly 1,800
seconds, which averages to ~109 ms/read against a BMC that answers ICMP in
~5 ms. That whole-run average was read as "every read pays ~99 ms of dead
time," which is not what the data actually shows once broken out by interval.
A later run's progress, sampled every few minutes, told a different story:
reads arrived in bursts of roughly 500 per 15 seconds — **~33 reads/sec, ~30 ms
per read** — separated by genuine multi-second stretches where the installer
issued zero reads at all (one interval showed +0 reads over 12 seconds). At
33 reads/sec, 16,451 reads is only about 8 minutes of actual reading inside a
much longer install; the rest of the time is the installer doing work that has
nothing to do with the media transport — decompressing squashfs, writing to
disk. **The install's duration is not dominated by this collection's network
path.** Retire the 109 ms figure; ~30 ms per read, arriving in bursts, is the
number to reason from.

**The A/B test.** The same install, same ISO, same machine, run once with
Nagle enabled and once with `TCP_NODELAY` set, compared at matching elapsed
times:

| elapsed | Nagle enabled | `TCP_NODELAY` set |
|---|---|---|
| 2.5 min | 967 reads / 23.5 MiB | 1030 reads / 25.1 MiB |
| 3.0 min | 1966 / 48.8 | 1996 / 49.5 |
| 3.5 min | 2860 / 71.3 | 2860 / 71.3 |
| 4.5 min | 3442 / 119.4 | 3442 / 119.4 |

(This table is for comparing the two runs against each other at matching
elapsed times — do not divide any row's MiB by its elapsed time to get a
throughput figure. Those elapsed times include the ~2-minute bootloader-menu
idle baked in; see "Measured performance and behaviour" above for the
correctly-windowed throughput numbers and the total-over-total trap that
produces a falsely low rate.)

The two runs stayed within ~2% of each other throughout, and were identical to
the byte from the 3.5-minute mark on. **`TCP_NODELAY` produced no measurable
speedup on this hardware. The Nagle hypothesis is falsified as the dominant
latency cause, not merely unconfirmed.** This is recorded as a negative result
on purpose, the same way the four wrong diagnoses above are recorded: so the
next person looking at read latency does not re-try this expecting it to
matter.

**Why the fix stays anyway.** Disabling Nagle on a strictly synchronous
request/response socket is still the correct default — it simply was not this
workload's bottleneck. `changelogs/fragments/tcp-nodelay.yml` describes it as a
socket-hygiene fix with a measured null result, not a performance win.

**What still explains the ~30 ms.** Network RTT to the BMC is ~5 ms, leaving
roughly 25 ms per read unaccounted for. The leading candidate is the BMC's own
USB-to-TCP relay turnaround on this 2014-era ASPEED AST2400 — no client-side
socket option can affect that half. Separating "our client is slow to
send/receive" from "the BMC is slow to relay" needs one measurement that has
not been taken yet: a timestamp immediately before and after the client's own
`send()` call (client-side delay), compared against the gap between that send
and the next inbound request arriving (BMC-side turnaround). Whichever side
that gap lands on is the next real lead — not another socket option.

## Still unproven — do not claim these

- **Whether the guest OS can obtain its own media session.** Once Linux boots it
  re-enumerates USB storage with its own driver, and `cd-media` allows exactly
  **one** session with **no** server-side timeout to reclaim an abandoned one. If
  our daemon holds the slot, the guest may be unable to reach its own media —
  failing *after* the installer appears to start. Partially addressed: a live
  Alpine booted from this path kept reading through the same held session past
  kernel handoff, so the session does appear to carry through to the guest. Not
  yet confirmed for a full installer that writes to disk.
- **KVM video decoding.** We have the greeting and the framing; no console frame
  has been decoded from this board.
- **Virtual floppy and virtual hard disk device classes.** The ports bind, but
  only CD-ROM has been exercised.
- **Any board other than this one.** One machine, one firmware version. This is
  repeatability at best, not a compatibility guarantee.
- **The cause of the residual ~30 ms per read** (network RTT to the BMC is
  ~5 ms). `TCP_NODELAY` was tried and produced no measurable change in a
  controlled A/B test, which rules it out as the dominant cause but does not
  identify the real one. The BMC's own USB-to-TCP relay turnaround on this
  2014-era AST2400 is the leading candidate, unmeasured directly — see
  "`TCP_NODELAY` on the media socket" above for the measurement that would
  settle it.
