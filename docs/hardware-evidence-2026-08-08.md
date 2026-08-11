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
- **The BMC's clock cannot be relied on, but it is not permanently wrong.** On
  2026-08-08 it reported `Thu Jan 25 17:40:30 2018`. On 2026-08-10
  `getdatetime.asp` reported the correct current date, with `TIMEZONE 'GMT+8'`.
  So the 2018 reading was a state it was in, not a fixed defect, and **what
  caused it is unexplained.** Do not rely on a BMC-supplied timestamp without
  checking it, and note that while the clock reads 2018 the BMC considers its
  own expired certificate still valid.

  One tempting explanation is recorded here only to warn against it: the
  firmware's own build stamp is `Jan 25 2018` at `17:49:02 CST`
  (`getfwinfo.asp`), nine minutes *after* that 2018 reading, which invites the
  theory that the clock resets to build time on power loss. That is **not
  established** — the timing is suggestive and nothing more, and the clock
  reading correctly two days later fits it poorly. Sourcing it would need the
  clock observed immediately after a deliberate power loss.
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

## A real installer reached 70% and then failed on media read timeouts

Observed 2026-08-09, from the physical console. **The furthest any install has
got, and the most informative failure recorded here.** Serving the
vendor-prepared auto-install ISO (`proxmox-auto-install-assistant prepare-iso
--fetch-from iso`, 1,628 MiB):

```
INFO: progress  70.2 % - extracting pve-firmware_3.18-3_all.deb
[  882.138239] I/O error, dev sr0, sector 1662408 op 0x0:(READ) flags 0x80700 phys_seg 2
[  882.142976] I/O error, dev sr0, sector 1662648 op 0x0:(READ) flags 0x84700 phys_seg 2
[  882.146847] I/O error, dev sr0, sector 1662888 op 0x0:(READ) flags 0x80700 phys_seg 2
[  882.149722] I/O error, dev sr0, sector 1662168 op 0x0:(READ) flags 0x800000 phys_seg 1
installation of package pve-firmware_3.18-3_all.deb failed
ERROR: Installation failed: low level installer returned early
Auto-installation failed (exit-code 1) - see above for errors.
root@pve-test:/#
```

**What this proves, and it is a lot.** The shell prompt reads `root@pve-test` —
the `fqdn` from `answer.toml`, applied by the installer. So the answer file was
found, parsed and acted on; disk targeting was reached; and several hundred
packages extracted successfully. This is **not** a configuration or answer-file
problem, and the earlier rounds of answer-file iteration are vindicated rather
than suspect.

**What failed: media reads went unanswered.** The decisive detail is an absence
— across the entire 60-minute session the client logged **zero
`REQUEST_SENSE` (0x03)** commands. When a SCSI device returns an error *status*,
Linux immediately issues `REQUEST_SENSE` to ask why. It never did. So the guest
was never told "error"; it simply never got replies in time and its SCSI layer
timed out. That distinguishes this sharply from the `READ TOC` bug above, where
we returned *wrong bytes*: here the bytes never arrived.

The sectors are in range and unremarkable. The kernel reports `sr0` in 512-byte
units, so sector 1662408 is LBA 415602 of an 833,632-LBA image — mid-image, not
past the end.

Session-side, the same run recorded **32,741 reads, 2,662 MiB served, `err=None`,
and zero unknown opcodes**. From our side it looked healthy throughout.

**Unexplained, and left unexplained on purpose.** The kernel timestamped those
errors at **882 s** — about 14.7 minutes after the installer kernel started — but
the console was observed showing 70% roughly **46 minutes** into the run, and our
reads continued to flow until minute 60. Those two facts have never been
reconciled. Do not construct a story that fits one and ignores the other; the
discriminator would be `dmesg` in full plus `date; cat /proc/uptime` from the
installer shell, mapping kernel time to wall clock. That was not captured.

**One contributing factor found later.** The host has **no Ethernet link on any
NIC** — the onboard Intel reports `PXE-E61: Media test failure, check cable` and
both ConnectX-3 Pro ports report `Link:down`. So `source = "from-dhcp"` in the
answer file could never have succeeded. That does **not** explain the `sr0`
errors, which are the media path, not the network — but it would have broken a
later stage regardless, and it explains why the partially-installed system was
never reachable afterwards.

**Why this failure class is worth engineering away rather than debugging.**
Proxmox's own netboot support (`prepare-iso --pxe --pxe-loader ipxe`) loads the
installer image into RAM as a second initrd before the kernel starts, so an
install performs **no CD reads at all**. A read timeout mid-extraction stops
being a bug to explain and becomes impossible by construction. See
[`netboot-design.md`](netboot-design.md).

## A sub-megabyte iPXE bootstrap boots over iUSB — confirmed

Observed 2026-08-09. This is the finding the "small bootstrap, bulk over HTTP"
approach depends on, and it holds.

A custom iPXE image built with an embedded script — **933,888 bytes, 0.89 MiB**
— was served as a virtual CD-ROM and booted by this board, twice in a row, from
a cold `reset`. Read trace of one boot:

| elapsed | reads | MiB | max LBA |
|---|---|---|---|
| 1.3 min | 22 | 0.04 | 34 |
| 2.0 min | 42 | 0.58 | 304 |
| 3.0 min | 42 | 0.58 | 304 |

Then reads **stop permanently**. That is the distinguishing signature: iPXE is
loaded wholly into RAM by the bootloader, so once it is running it never touches
the media again — unlike an OS installer, which reads throughout its run. Only
~0.58 MiB of the image is ever read (the El Torito boot image, not the whole
ISO), and the elapsed time is dominated by POST, not transfer.

Opcode profile across two boots, all handled, none unknown:

```
TEST_UNIT_READY=49, READ_CAPACITY10=2, READ10=104, READ_TOC=3,
AMI_ACK=1, AMI_CTRL_F3=4
```

**Why this matters.** Against a 1,628 MiB installer ISO at ~790 KB/s (~35
minutes, and a real attempt failed partway), a 0.89 MiB bootstrap is ~1.2
seconds of transfer. The BMC's throughput ceiling stops being a problem to
solve and becomes a constraint to route around.

**What is NOT yet proven: the HTTP hand-off.** With an ephemeral HTTP origin
confirmed listening on the controller and reachable from the controller's own
LAN address, **no request from the booted iPXE ever arrived.** So iPXE runs but
does not reach the origin. Unresolved, and worth noting the topology: the BMC,
the host NIC, and the controller are each on different VLANs routed through a
common gateway, and a sibling design elsewhere sets a static network tuple in
its bootstrap precisely because an earlier attempt failed to obtain a DHCP
lease. Whether this is a missing lease, a routing/filtering boundary, or a
script fault is undetermined; the console holds the answer and has not been read.

### An instrumentation failure worth recording

Three earlier runs of this same test reported `reads=0` and were read as "the
host never touched the disc." **That conclusion was false and the cause was the
measuring instrument.** The harness's frame hook is called as
`hook(direction, data)` — `direction` being the string `"rx"`/`"tx"`, with the
SCSI opcode at `data[32:][9]`. The test's hook assumed
`hook(opcode, payload)`, so its counters were keyed by `"rx"`/`"tx"` and the
`op == 0x28` comparison could never be true. The counter was **structurally
incapable of registering a read**, and its zero carried no information at all.

Two boot cycles and a wrong diagnosis (a "wedged" media slot, plus an
unnecessary BMC cold reset) were spent on that zero. The rule it earns:
**before concluding that hardware did nothing, confirm the instrument can
register a positive.** A single known-good comparison — here, the working
harness in the same directory — would have exposed it immediately.

## Per-read latency is a tail, not a fixed penalty

Measured 2026-08-10, and it **replaces the earlier model**. Previous entries
described "~30 ms per read" as though every read paid a fixed cost. That figure
came from dividing reads by elapsed time, so it was a mean, and a mean is the
wrong summary for this distribution.

Timestamping every inbound `READ10` and taking gaps between consecutive reads,
excluding gaps over 500 ms as genuine inter-phase idle (n=39, bootloader phase,
0.89 MiB image):

| statistic | value |
|---|---|
| minimum | **7.8 ms** |
| p10 | 8.8 ms |
| median | **15.7 ms** |
| mean | 30.6 ms |
| p90 | 79.2 ms |
| maximum | 148.0 ms |

**The floor is ~8 ms, against a network RTT of ~5 ms.** So when the BMC is warm
it turns a read around in close to network time, and there is no fixed
per-read penalty to remove. The mean is dragged up by a long tail: the p90 is
five times the floor. Whatever the cause is, it is **intermittent** — variable
BMC-side work, scheduling, or contention — not a constant.

That reframes the problem. Hunting for a setting that removes a fixed delay is
looking for something that does not exist; the question is what produces the
tail.

**Power Save Mode: inconclusive, leaning negative.** The BMC's virtual-media
"Power Save Mode" was disabled before this measurement, on the theory that a
sleeping USB relay would explain both the latency and the read timeouts that
killed an install. The mean afterwards (30.6 ms) is indistinguishable from the
~30 ms measured before it, which argues it is not the cause — **but this is not
a clean A/B and must not be recorded as one.** The two figures come from
different phases (bootloader single-sector probes here, versus OS bulk
multi-block reads before), different sample sizes, and different measurement
methods. A clean test is available and cheap: re-enable the setting and re-run
the identical probe against the identical image.

## Redirection rejection status `3` means "bad token"

Observed and then **resolved** 2026-08-09. Recorded in full because the
diagnosis went wrong twice before going right, and the wrong turns are the
instructive part.

```
AckError: vmedia: redirection not accepted (status 3)
```

**The cause was a bad token, and the bug was in a diagnostic harness, not in
this collection.** A JNLP's `<argument>` elements are a flat `-flag value`
list. The harness read the token positionally, as the fourth argument. On this
firmware that slot holds `-hostname`'s value — **the BMC's own IP address** — so
every attach authenticated with an IP address where a 16-character token
belonged, and the BMC refused. The shipped parser
(`asp.parse_jnlp_arguments`) has always resolved by flag name and was never
affected; a regression test now pins that, using this firmware's real argument
order.

The tell was in the logs the entire time and went unnoticed for hours: the
working run printed `token acquired (16ch)`, every failing probe printed
`token 13ch`. Thirteen characters is the length of a dotted-quad address.
**When a protocol rejects a credential, compare the credential's shape against
a known-good run before theorising about state.**

`3` therefore joins the sourced status values — `CONN_OK = 1`,
`CONN_ERR_IN_USE_5 = 5`, `CONN_ERR_IN_USE_8 = 8` — as an authentication
rejection rather than an occupancy one. That distinction matters: it is **not**
a busy signal, so retrying, waiting, or reclaiming sessions will never clear it.

**Two wrong diagnoses, both worth recording.** First, the failure was blamed on
a media session orphaned when a network outage cut the controller off
mid-install — plausible, because `cd-media` allows one session with no
server-side timeout and this collection cannot evict a session it does not
track. Second, when a BMC cold reset did not help, it was blamed on that reset
having reverted manually-set media settings. Both were consistent with the
evidence and both were wrong. The JNLP dump that settled it — printing every
argument with its index and length — cost one command and would have been the
correct first move. Nothing was ever wedged, and **the cold reset was
unnecessary**.

**The failure signature is genuinely easy to misread**, and that part stands
regardless of cause. The symptoms were:

- TCP to port 5120 **connected** and stayed `ESTABLISHED`
- 62 bytes sat **unread** in the socket's receive queue
- **zero** SCSI commands were ever serviced
- the log line `vmedia: redirection accepted (instance 0, port 5120)` — present
  in every successful run — was **absent**

So *an established connection is not evidence that media is being served.* The
presence or absence of that "redirection accepted" line is the discriminator,
and it is the thing to check first when media appears attached but the host
reads nothing. A 30-second attach-only probe distinguishes this from every
other failure mode without spending a boot cycle, and is far cheaper than
inferring from a read trace.

After correcting the token lookup, the same probe returned
`redirection accepted (instance 0, port 5120)` with no exception, immediately
and repeatably.

**Settings confirmed intact along the way.** The JNLP dump also reports the
board's live redirection configuration, which is worth knowing since it was
briefly suspected: `-cdstate 1` (CD redirection enabled), `-vmsecure 0` (media
encryption off), `-singleportenabled 0`, `-cdport 5120`, `-cdnum 1`. A BMC cold
reset did **not** revert any of these. That dump is the cheapest way to read
this configuration and needs no web UI.

Also recorded from that cold reset, since it is directly useful: the host was
**unaffected** and stayed powered on, and recovery was **staged** — ICMP and
IPMI (UDP 623) answered several minutes before TCP 443 did. A readiness check
keyed on ping therefore reports success while the web stack is still down.

## Authenticated `GET` reads require `CSRFTOKEN` on some endpoints — resolved 2026-08-11

Reported as [GitHub issue
#5](https://github.com/james-crowley/ansible-collection-asmb8-ikvm/issues/5), live-hardware
evidence from the same target board (firmware 1.14, aux 1.14.2), reported by the maintainer who
owns access to it. Recorded here in full because it retires a specific wrong inference this project
made elsewhere in its own code and docs, and this log exists precisely so a wrong turn is recorded,
not quietly deleted.

**The finding.** An authenticated `GET` against five endpoints —
`getalllancfg.asp`, `getlanchannelinfo.asp`, `getdnscfg.asp`, `getnwbondcfg.asp`, `checknwbond.asp`
— returned HTTP 200 with a byte-identical, 2,223-byte HTML page (SHA-256
`7129528f34a2b230534e705ad8cb230cd1f5d4ae0362a9f9694c99b61f4c3427`) containing HTML/login markers
and no `WEBVAR_JSONVAR_*`, even with a session freshly authenticated moments before. The identical
request, plus the `CSRFTOKEN` header harvested from the login response, got back an ordinary
`WEBVAR_JSONVAR_*` response every time, on every one of the five endpoints.

**The inference this project had drawn from an earlier, partial sample was wrong, and this is why
that matters.** Before issue #5, this project's own code comments and docs (`module_utils/asp.py`,
`docs/protocol-notes.md`) asserted that this collection's `GET` reads "already work without"
`CSRFTOKEN`, and that whether this firmware enforces the header at all was "unverified" as a
blanket statement about `GET`. That claim was built by testing a handful of endpoints by hand —
`getvmediacfg`, `getallservicescfg`, `getdatetime`, `getntpcfg` — none of which happen to enforce
the header, and generalising "works without it" from that sample to `GET` reads in general.
**Enforcement turned out to be per-endpoint, not a property of the HTTP method.** Four endpoints
tested by hand did not care about the header; five endpoints tested later did, every time. The
lesson generalises beyond this one header: a handful of endpoints behaving consistently is evidence
about those endpoints, not about the surface as a whole, and this project had already been burned
by exactly this shape of over-generalisation once before (see "READ TOC must honour the CDB
allocation length" above, on a different surface).

**The fix.** `AspClient._headers()` now attaches `CSRFTOKEN` to every non-`WEBSES` request,
`GET` included — matching the vendor JavaScript's own URL-based rule in `lib/xmit.js` exactly,
rather than the narrower, GET-excluded rule this project had previously implemented on the
assumption above.

**A second thing this same report resolved: `getremotesession.asp`'s "for reasons not yet
identified" session-expired quirk.** `asmb8_info.py` and `asmb8_sessions.py` both recorded that this
one endpoint answers a programmatic client with a session-expired HTML page even immediately after
a fresh, successful login, working fine from a browser, "for reasons not yet identified". The
missing `CSRFTOKEN` header is that reason, as a general mechanism — but `getremotesession.asp`
itself was **not** one of the five endpoints issue #5 tested, so whether it specifically enforces
`CSRFTOKEN` is still not confirmed either way. Both modules continue to treat a parse failure on
that one endpoint as an expected, degraded (not fatal) outcome; see each module's own documentation
for the corrected framing.

**A false-positive this collection produced, now closed.** Before this report, `AspClient` had no
detector at all for the session-expired HTML shape: `AspClient.get_host_status()` returned it
verbatim, so `asmb8_info(include_web_session=true)` could report `logged_in: true` alongside a
`host_status_raw` that was really this page — a confident wrong answer, worse than a hard failure.
`module_utils/asp.py`'s `looks_like_session_expired_html()` now recognises this shape *structurally*
(an HTML document carrying a login/session marker, with none of this format's own
`WEBVAR_JSONVAR_` marker) — deliberately not by the 2,223-byte length or the SHA-256 above, since
either would be a property of one firmware build's rendering of this one page and would silently
stop matching the moment a firmware revision changed so much as a whitespace character in a page
this project does not control. `get_host_status()`, `get_webvar()`, `post_webvar()`, and
`set_webvar()` all check for it now, and none of this collection's ~58 real, redacted WEBVAR/JSONVAR
fixtures under `tests/unit/fixtures/asp/` trip it.

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
- **The HTTP hand-off from a booted iPXE bootstrap.** iPXE itself boots over
  iUSB (confirmed above, twice), but no request from it has ever reached an
  HTTP origin on the controller. Cause undetermined — missing DHCP lease,
  routing/filtering between VLANs, or a fault in the embedded script. Until
  this works, the bootstrap approach is half-proven: the hard part (does the
  board boot a tiny image over a slow channel?) is answered yes; the easy-
  sounding part is not.
- **The cause of the read-latency tail.** `TCP_NODELAY` was tried and produced
  no measurable change in a controlled A/B, which rules it out. The BMC's own
  USB-to-TCP relay turnaround remains the leading candidate. See "Per-read
  latency is a tail, not a fixed penalty" below for what is now measured and
  what is still open.
