<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Ansible Collection: `james_crowley.asmb8_ikvm`

Out-of-band management of **ASUS ASMB8-iKVM** BMCs (AMI MegaRAC firmware on an
ASPEED AST2400) from Ansible: power control and one-time boot selection
wrapping IPMI, plus **streaming a local ISO straight from the Ansible
controller to the BMC's virtual CD-ROM** over AMI's proprietary iUSB protocol
— so a bare-metal host can boot from it with **no PXE, DHCP, TFTP, NFS, or
CIFS infrastructure required.**

The motivating use case is the same shape as PXE-based bare-metal
provisioning, without needing to stand up or maintain that infrastructure:
attach an installer ISO directly to a machine's virtual CD-ROM, arm a
one-time boot over IPMI, reset, and let it install. Then hand off to ordinary
Ansible roles once the OS is up.

## Why this exists

This board has **no Redfish.** That is not a firmware gap to be fixed — it is
generational: Redfish arrived with the AST2500/ASMB9 family, and the target
hardware here is an AST2400/ASMB8. Standard IPMI, in turn, has no
virtual-media capability at all. Between those two facts there is no standard
API for attaching boot media to this board, which is the entire reason the
protocol work in this collection exists: AMI's **iUSB** protocol, layered on
top of the BMC's legacy `.asp` web-management session, is the only path to
virtual media this generation of hardware offers, and AMI has never published
a specification for it.

Power control and one-time boot selection are a different story: IPMI
(chassis control, boot-flags) is well supported, well understood, and already
correctly implemented by `pyghmi` (which is also what
`community.general.ipmi_power`/`ipmi_boot` use internally). This collection
does not reinvent that path — `asmb8_power` and `asmb8_boot` are thin,
classified wrappers over it. All of the genuinely new work in this collection
is in the iUSB virtual-media client.

**What is genuinely new here, and what is not.** This collection is not the
first tool to speak iUSB. Its implementation was built by reverse engineering,
informed by a third-party Go client for a related AMI MegaRAC board
(`BadCoder1337/rd450x-console`, MIT-licensed), an older Lua/Python
reverse-engineering artifact for a different device class
(`samozy/iusb`), and — decisively, for the facts those two got wrong for this
specific board — a local, non-redistributed decompilation of the vendor's own
JViewer client retrieved from the target hardware. See [NOTICE](NOTICE) for
the full, per-file provenance accounting: which files were consulted, what was
taken from each, and where they disagreed and which one won. What is new: a
pure-Python iUSB client, callable directly from an Ansible module, with no
external binary, no Java runtime, and no JViewer applet anywhere in the path.

See [`docs/protocol-notes.md`](docs/protocol-notes.md) for the normative wire
format this implementation is built against.

## Project status

**0.1.0 — not hardware-qualified.** This is not a hedge; it is the accurate
description of where this collection is. Read
[`docs/hardware-evidence-2026-08-08.md`](docs/hardware-evidence-2026-08-08.md)
for the full, dated, falsifiable record everything below is drawn from, and
[`docs/capability-matrix.md`](docs/capability-matrix.md) for the complete
claim-by-claim accounting.

**What is proven**, against one ASUS Z10PE-D16 WS / ASMB8-iKVM board on
firmware 1.14 (aux 1.14.2), on 2026-08-08:

- The iUSB authentication handshake, byte-exact: the 32-byte header, the
  128-byte auth payload, the ACK, and the connection-status semantics.
- A full CD-ROM emulation exchange over that session — `TEST UNIT READY`,
  `READ CAPACITY(10)` (verified byte-exact against a 1052-sector test image),
  `READ(10)`/`READ(12)` — with no Java anywhere in the path.
- The El Torito boot chain actually followed by the firmware: LBA 0, 1, 16,
  17, the boot catalog at LBA 4660, the terminator at 18, then the BIOS image
  at LBA 6025 and the UEFI image at LBA 156 — matching `xorriso
  -report_el_torito` on the same image exactly.
- The bootloader then loading and streaming a real Proxmox VE 9.2-1 ISO:
  multi-block reads up to 16 blocks (32 KiB). The final tally for that run was
  2,839 read requests and 33,038 sectors (~71.0 MiB) served.
- Correct handling of a high LBA (832,880 of an 833,095-sector image) — ruling
  out a 16-bit truncation bug.
- A one-time IPMI boot-device override, and confirmation that it reverted to
  `default` after the following reset (true one-time semantics, not a
  persistent change).
- The media session **surviving a host power cycle** — it stayed
  authenticated across a reset and kept serving.
- IPMI power state and boot-device reads/writes over `pyghmi`, working
  cleanly with no special flags.

**What is not proven** — and must not be claimed as working until it is:

- **A completed unattended OS install.** The furthest this collection has
  reached is the installer streaming its squashfs; no install has been driven
  to completion.
- **Whether the guest OS can obtain its own media session.** Linux
  re-enumerates USB storage with its own driver once it boots, and this
  board's `cd-media` service allows exactly **one** session with **no**
  server-side timeout to reclaim an abandoned one. If this collection's own
  daemon is still holding that slot, the guest OS may be unable to reach its
  own media — a failure that would surface *after* the installer appears to
  start. Untested.
- **KVM video decoding.** The IVTP greeting and framing are understood; no
  console frame has been decoded from this board.
- **The virtual floppy and virtual hard-disk device classes.** Their ports
  bind, but only CD-ROM has been exercised.
- **Any board other than this one.** One machine, one firmware version. This
  is repeatability at best, not a compatibility guarantee.

## Known limitations

Each of these is a measured or reasoned limit, sourced from
[`docs/hardware-evidence-2026-08-08.md`](docs/hardware-evidence-2026-08-08.md)
unless stated otherwise, not a hedge.

### The factory certificate cannot be chain-validated, ever

The BMC's TLS certificate is self-signed *and* already expired: subject and
issuer are both `C=US, O=American Megatrends Inc, OU=Service Processors,
CN=AMI`, valid 2016-06-01 to **2026-05-30**. Chain validation (`ca_path`,
`validate_certs=true`) cannot succeed against this board's factory
certificate under any circumstances. Fingerprint pinning
(`tls_fingerprint`) is the only trust mode that actually works here without
replacing the certificate on the BMC.

### TLS 1.2 with one static-RSA cipher — Python fails, `curl` misleadingly succeeds

The BMC's TLS listener offers **TLS 1.2 only** (1.0, 1.1, and 1.3 are all
refused at the handshake) and **exactly one ciphersuite**,
`AES256-GCM-SHA384` — static-RSA key exchange, no forward secrecy. Modern
OpenSSL/Python builds exclude non-forward-secret ciphersuites from their
default list, so a plain `requests.get(...)` against this BMC fails the
handshake outright. `curl` is more permissive by default and will *appear* to
work against the same endpoint — that is not evidence of a bug in this
collection; it is a difference in default cipher policy between HTTP clients.
This collection's own client (`plugins/module_utils/asp.py`) restores the
required cipher itself, so this is transparent to a playbook.

### The BMC's clock is wrong

The BMC reported `Thu Jan 25 17:40:30 2018` while actually running in 2026.
Never trust a BMC-supplied timestamp for anything — including its own view of
whether its (already-expired) certificate is currently valid.

### The web server hangs rather than refuses when its worker pool saturates

The BMC's web server is HTTP/1.0 with no keep-alive, caps at 20 concurrent
sessions, and runs a **separate worker pool per listener** (plain web UI,
`.asp` RPC, iUSB/media). Concurrent load exhausted the port-80 pool during
testing: the BMC kept completing TCP handshakes while never serving a single
request on that pool, for several minutes, while port 443 stayed perfectly
responsive throughout. This is a hang, not a clean refusal, and it is the
reason `ErrorClass.BMC_BUSY` exists (see [Error handling](#error-handling))
and why this collection serializes every request to a given BMC rather than
issuing them concurrently. It is also why HTTPS, not plaintext HTTP, is this
collection's supported transport — nothing about the worker-pool behaviour is
specific to one listener, but only the TLS listener has actually been proven
resilient to it.

### The virtual-media slot is single-occupancy, board-wide, with no timeout

The `cd-media` service allows **exactly one active session** for the entire
board, and has **no server-side timeout** to reclaim an abandoned one. A
session left open by an unclean shutdown holds that slot forever until
something closes its TCP connection — there is no remote "kick the current
holder" call this collection can send, because none exists in the observed
protocol. `asmb8_media`'s attach flow always attempts to reclaim every session
its own `runtime_dir` still has a record of before opening a new one (see
[Virtual media](#virtual-media)), but that can only reclaim what it knows
about. A BMC cold reset (`ipmitool mc reset cold`, or the `pyghmi`/
`community.general.ipmi_power` equivalent) is the operator's escape hatch when
nothing else clears it — it resets the management controller only, not host
power.

### Idle is normal and has no meaningful upper bound

A healthy, attached media session went completely silent for **130
consecutive seconds** while the host sat at a bootloader menu, then resumed
serving reads normally with no intervention. There is no idle duration this
collection treats as failure — a host can sit at an installer prompt or a
firmware setup screen indefinitely, and a long, quiet wait is not evidence of
a hang.

### Throughput is slow: budget minutes, not seconds

Measured throughput was **≈800–900 KB/s** with 16-block (32 KiB) reads.
Proxmox's own `pve-installer.squashfs` is **614 MB** on its own — that is
**13+ minutes of streaming for one file**, before the installer even starts
running against it. Every timeout in this collection that touches the media
path is sized with that in mind; do not shrink `attach_timeout`,
`handoff_timeout`, or similar values casually.

### A stock installer ISO will NOT install unattended — the GRUB menu trap

**Booting an unprepared installer ISO does not produce an unattended
install.** A stock Proxmox VE ISO's own `grub.cfg` sets its boot `timeout`
only inside a conditional that is true only when `auto-installer-mode.toml`
is present at the ISO root:

```
if [ -f auto-installer-mode.toml ]; then
    set timeout-style=menu
    set timeout=10
    menuentry 'Install Proxmox VE (Automated)' ... proxmox-start-auto-installer
fi
```

A stock ISO lacks that file, so there is **no timeout at all** — the boot menu
waits forever, confirmed both from the iUSB read trace (loading halted after
~2.8 MB) and by direct observation of the console. The supported unattended path is
an ISO already prepared with `proxmox-auto-install-assistant prepare-iso`
(which bakes in an answer file and flips the ISO's own GRUB config to boot its
`Install Proxmox VE (Automated)` entry without a keypress) — point
`asmb8_media`'s `image` option at the *output* of that command, never at the
stock ISO. `asmb8_media` has no answer-file/floppy slot of its own; the answer
file must already be inside the ISO you hand it. The
[`asmb8_autoinstall_iso`](roles/asmb8_autoinstall_iso/README.md) role
automates exactly that preparation step (rendering and validating an
`answer.toml`, then baking it into a copy of the ISO via the vendor tool or a
container-based `xorriso` fallback) — see
[`docs/proxmox-autoinstall.md`](docs/proxmox-autoinstall.md) for the evidence
behind it.

### The KVM console channel opens, but nothing decodes video — and part of its own documentation is unverified

`asmb8_redirection` completes the real IVTP handshake and can save one raw
video frame's still-encoded bytes, but this collection implements **no** part
of the AMI/ASPEED video codec — `capture=decoded_frame` fails outright with
`error_class=unsupported_capability` rather than faking a decode. Unlike
every other module, `asmb8_redirection` has **zero** live-hardware evidence
and **zero** unit/mock test coverage behind it: everything it does is sourced
from decompiled vendor client analysis alone, including at least one specific
wire-format detail (a `pktSize` field this collection writes as 324 bytes,
where the decompiled client itself inconsistently writes 332) that is flagged
in its own source as unverified against real hardware. Its claimed KVM
service capacity — 4 concurrent sessions, an 1800-second inactivity timeout —
is also unsourced beyond the module's own documentation. See
[`docs/asmb8_redirection.md`](docs/asmb8_redirection.md) and
[`docs/capability-matrix.md`](docs/capability-matrix.md) Tier 4 for the full
accounting.

## Requirements

- **Controller**: Python 3.10+ with `requests>=2.25.0` and `pyghmi>=1.5.0`.
  Install with `pip install -r requirements.txt`.
- **ansible-core >= 2.17.** The sanity-test boilerplate requirement changed
  incompatibly at 2.17 — no single module form is sanity-clean on both an
  older and a newer `ansible-core`, so 2.17 is the floor rather than a choice.
- **Target**: nothing. No agent, no SSH, no Python interpreter on the managed
  node. The BMC is firmware, reachable independently of the host OS.
- An ASMB8-iKVM BMC that is **reachable and has a known admin credential**.
  This collection manages the BMC; it does not provision or factory-reset it.
- `community.general` is **not** a dependency (`galaxy.yml`'s `dependencies:
  {}` is intentional). This collection calls `pyghmi` directly rather than
  wrapping `community.general.ipmi_power`/`ipmi_boot`, specifically so it does
  not need a whole other collection as a dependency just to reach a library
  it would end up importing anyway.

## Installation

Not yet published to Ansible Galaxy. Install from source:

```bash
ansible-galaxy collection install git+https://github.com/james-crowley/ansible-collection-asmb8-ikvm.git
pip install -r requirements.txt
```

## Modules

| Module | Purpose | Mutates? | Status |
|---|---|---|---|
| [`asmb8_info`](docs/asmb8_info.md) | IPMI-observed capability/state facts, plus optional read-only `.asp` diagnostics | No | Implemented |
| [`asmb8_power`](docs/asmb8_power.md) | Power control over IPMI, wrapping `pyghmi` directly | Yes | Implemented |
| [`asmb8_boot`](docs/asmb8_boot.md) | One-time IPMI boot-device override; persistent changes are refused outright | Yes | Implemented |
| [`asmb8_media`](docs/asmb8_media.md) | Stream a local ISO to the BMC's virtual CD-ROM over iUSB | Yes | Implemented |
| [`asmb8_redirection`](docs/asmb8_redirection.md) | Open the iKVM console (IVTP) session headlessly: confirm the channel is live, or save one raw (undecoded) video frame | No (`changed` is always `false`) | Implemented, but **less proven than the other four** — see below |

All five are named in `meta/runtime.yml`'s `asmb8_ikvm` action group and
implemented. Set their shared connection options centrally with
`module_defaults`:

```yaml
module_defaults:
  group/james_crowley.asmb8_ikvm.asmb8_ikvm:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
```

`asmb8_power` and `asmb8_boot` accept every option in that fragment (for
`module_defaults` compatibility with the group above) but only actually use
`host`, `username`, `password`, and `ipmi_port` — the rest describe the `.asp`
web-management surface, which IPMI does not touch. See each module's own
`DOCUMENTATION` for the exact split.

## Transport and trust

The `.asp`/JNLP web-management plane (used by `asmb8_info`'s optional
`include_web_session` and by `asmb8_media`'s attach flow) defaults to HTTPS on
port 443, and requires an explicit trust decision:

- **`tls_fingerprint`** — SHA-256 leaf pinning. **This is the recommended, and
  in practice the only working, trust mode for this board**: its factory
  certificate is self-signed and already expired (see [Known
  limitations](#the-factory-certificate-cannot-be-chain-validated-ever)), so
  chain validation cannot succeed against it regardless of `ca_path`.
- **`ca_path`** — ordinary chain and hostname verification. Kept as a real,
  working mode for the day this board's factory certificate has been replaced
  with one issued by an actual CA — not the expected posture for the hardware
  this collection targets today.
- **`allow_insecure_transport=true`** paired with `use_tls=false` — plaintext
  HTTP, never selected implicitly. The session cookie and the iUSB/KVM media
  token both cross the network recoverable by an on-path attacker when this is
  set; only use it on an isolated management VLAN.

IPMI (`asmb8_power`, `asmb8_boot`, and `asmb8_info`'s IPMI facts) has no TLS
layer of its own and is unaffected by any of the above — it is plain
UDP 623, using `pyghmi`'s defaults, with its own `ipmi_port` option.

This collection's HTTP client re-enables the cipher this board's TLS listener
actually requires (see [Known
limitations](#tls-12-with-one-static-rsa-cipher--python-fails-curl-misleadingly-succeeds))
transparently — you do not need to do anything for it to work once TLS is
otherwise configured correctly.

## Virtual media

`asmb8_media` streams a local ISO from the controller to the BMC's virtual
CD-ROM over iUSB. Three things to know before using it:

1. **It is always read-only.** The CD-ROM channel this collection speaks has
   no write opcode at all in the BMC's own firmware (confirmed by
   disassembling the vendor's own SCSI dispatcher — see
   [NOTICE](NOTICE)); there is no writable-image option to offer, unlike the
   sibling `james_crowley.intel_amt` collection's floppy/USB-R slot.
2. **A session is long-lived and the module call is not.** `state=attached`
   forks a detached background process that owns the connection and streams
   the ISO for as long as the install takes (potentially over an hour); the
   module invocation itself returns once that process reports `attached` or
   an early failure. `session_id` is how you refer to it again later —
   capture it if you will need to `state=detached` it.
3. **The single-session hazard is real and always handled, not just
   documented.** Every `state=attached` call attempts to reclaim every *other*
   session this collection's own `runtime_dir` still has a record of against
   the same endpoint before ever opening a new connection — live or stale,
   whether or not this particular call is the one that ends up failing. See
   [Known
   limitations](#the-virtual-media-slot-is-single-occupancy-board-wide-with-no-timeout)
   for what that reclamation can and cannot reach.

`cd_port` (default 5120) is a *separate* TCP listener from `port` (the
BMC's HTTPS web-management port, default 443): `port` is used to log in and
fetch the JNLP document that mints a media session token; `cd_port` is where
the ISO bytes actually stream. On the target hardware, `cd_port` refuses
connections outright until a JNLP fetch allocates a session — a closed
`cd_port` before any attach is normal, not a fault.

## Example: unattended bare-metal install

Every module and option name below is real and exists in this collection's
code today. Note `delegate_to: localhost` on every task: an ASMB8-iKVM BMC is
firmware and cannot execute a Python payload, so every module in this
collection runs on the **controller**, never on the managed node.

```yaml
- name: Install an operating system onto bare metal via virtual media
  hosts: "{{ target }}"
  serial: 1                     # never fan out a reset
  gather_facts: false
  connection: local
  module_defaults:
    group/james_crowley.asmb8_ikvm.asmb8_ikvm:
      host: "{{ asmb8_host }}"
      username: "{{ asmb8_username }}"
      password: "{{ asmb8_password }}"
      tls_fingerprint: "{{ asmb8_tls_fingerprint }}"

  tasks:
    - name: Attach an already-prepared installer ISO to the virtual CD-ROM
      # See "A stock installer ISO will NOT install unattended" above --
      # this must be the output of `proxmox-auto-install-assistant prepare-iso`
      # (or the asmb8_autoinstall_iso role, which automates that step), never
      # a stock ISO.
      james_crowley.asmb8_ikvm.asmb8_media:
        image: /srv/images/proxmox-ve-auto.iso
        state: attached
      delegate_to: localhost
      no_log: true
      register: media

    - name: Arm a one-time optical boot and reset into the attached ISO
      james_crowley.asmb8_ikvm.asmb8_boot:
        device: optical
      delegate_to: localhost
      no_log: true

    - name: Reset into the installer
      james_crowley.asmb8_ikvm.asmb8_power:
        state: reset
      delegate_to: localhost
      no_log: true

    - name: Wait for the installed OS to come up
      ansible.builtin.wait_for:
        host: "{{ provisioned_host }}"
        port: 22
        timeout: 3600     # budget minutes, not seconds -- see Known limitations
        delay: 120

    - name: Detach the media
      james_crowley.asmb8_ikvm.asmb8_media:
        session_id: "{{ media.session_id }}"
        state: detached
      delegate_to: localhost
      no_log: true
```

The `asmb8_baremetal_install` role (see [`roles/asmb8_baremetal_install/README.md`](roles/asmb8_baremetal_install/README.md))
wraps this same sequence with a fan-out guard, an explicit destructive-action
confirmation, resumability across interrupted runs, and a guaranteed media
detach on failure — read that role's own README before pointing it at real
hardware; **nothing in that role has completed a full, real OS install yet
either.**

## Idempotence and check mode

| Module | Reads before writing | `changed` fires when | Check mode |
|---|---|---|---|
| `asmb8_info` | n/a (read-only) | never (`changed=false` always) | Full read, identical to normal mode |
| `asmb8_power` | Yes (`get_power_state`) | Requested state differs from observed (`on`/`off`); always for `shutdown`/`reset`/`boot`, which can never compare equal to a reported state | Reports the plan; never sends the IPMI command |
| `asmb8_boot` | Yes (`get_boot_device`) | Requested `device`/`uefi` differs from the current override | Reports the plan; never sends `set_bootdev()` |
| `asmb8_media` attach | Yes (session state file) | A new background process was actually forked (or, in check mode, would be) | Validates the image and reports the plan; never forks, signals another session, or contacts the BMC |
| `asmb8_media` detach | Yes (session state file) | A live process was actually asked to stop (or, in check mode, would be) | Reports whether a live session would be stopped; never signals it |

Two design commitments that hold across every module here:

- **An uncertain mutation is never retried automatically.** A timeout *after*
  a request was transmitted is reported with `indeterminate=true`, so a caller
  re-probes rather than blindly retrying a reset or attach that may have
  already taken effect.
- **One-shot boot is never silently re-armed persistently.** `asmb8_boot`
  refuses `persistent=true` outright, before any IPMI session is even opened
  — see its own documentation.

## Error handling

Every failure carries a stable, machine-readable `error_class`
(`plugins/module_utils/errors.py`):

`connection`, `tls_validation`, `authentication`, `unsupported_capability`,
`invalid_state`, `timeout`, `protocol`, `remote_operation`,
`identity_mismatch`, and **`bmc_busy`** — the one class this collection added
beyond the taxonomy it shares with the sibling `james_crowley.intel_amt`
collection, specifically because this board's web server hangs rather than
refuses under load (see [Known
limitations](#the-web-server-hangs-rather-than-refuses-when-its-worker-pool-saturates)),
and because the single-occupancy media slot's rejection is a distinct,
nameable condition rather than an ordinary timeout.

Every user-visible message and diagnostic is passed through
`errors.redact()` before it reaches a module result: session cookies, the
`WEBVAR_PASSWORD` login field, iUSB/KVM tokens, `Authorization`/`Cookie`
headers, and generic `password=`/`token=`-shaped values are all stripped.
Diagnostics are also length-bounded so a full HTTP body or JNLP document never
lands in a task result verbatim.

## Testing

```bash
COLLECTION_PATH="$(./scripts/setup-collection-tree.sh)"
cd "$COLLECTION_PATH"
ansible-test sanity --venv --python 3.12
ansible-test units  --venv --python 3.12
ansible-test integration --venv --python 3.12   # against local mock .asp/iUSB servers
```

See [`docs/testing.md`](docs/testing.md) for the full pytest inner loop, the
mock server fault-injection modes, the lint commands, and the CircleCI
job/workflow layout — including the approval-gated hardware-in-the-loop chain
that this collection has **not yet run**.

## Security notes

BMC credentials are equivalent to physical access to the machine: someone
holding them can power the machine on or off, force a boot-device override,
and — with `asmb8_media` — boot it from media of their own choosing,
regardless of what the installed OS wants. See [SECURITY.md](SECURITY.md) for
the full policy. Specific to this collection's current state:

- The factory TLS certificate cannot be chain-validated (see [Known
  limitations](#the-factory-certificate-cannot-be-chain-validated-ever));
  fingerprint pinning is the only trust mode that actually works, and
  plaintext transport requires an explicit, never-implicit opt-in.
- `password` is `no_log` in every module's argument spec; every example in
  this repository also sets task-level `no_log: true` as defence in depth.
- Credentials, session cookies, and iUSB/KVM tokens are never written to
  operation receipts, facts, or the `asmb8_media` session state file — see
  `plugins/module_utils/models.py`'s `JnlpSession` docstring for exactly which
  fields are secret-shaped and how this collection avoids letting them leak
  into a return value.
- The iUSB protocol's own authentication and confidentiality properties are
  not independently verified beyond what riding on top of the `.asp` web
  session provides — see `docs/protocol-notes.md` for exactly what has and has
  not been established about the wire format itself.

## License and attribution

GPL-3.0-or-later. See [LICENSE](LICENSE).

This collection's iUSB implementation draws on
[`BadCoder1337/rd450x-console`](https://github.com/BadCoder1337/rd450x-console)
(MIT-licensed — see [`licenses/MIT.txt`](licenses/MIT.txt)) and on a local,
non-redistributed decompilation of the vendor's own JViewer client retrieved
from the target hardware, used solely to understand an undocumented protocol
well enough to interoperate with it. **Neither the vendor binaries nor any
decompiled output is redistributed in this collection.** Full per-file
provenance — which sources were consulted, what was taken from each, and
every point where they disagreed and which one won — is in [NOTICE](NOTICE).

## Contributing

Issues and PRs welcome, including issues that are just "here is hardware
evidence" or "here is a packet capture" — see the feature-request template.
Conventional commits; every user-facing change needs a changelog fragment in
`changelogs/fragments/`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the local verification sequence
and the practical tooling traps this project shares with
`james_crowley.intel_amt`, and [`SECURITY.md`](SECURITY.md) for why this
collection warrants unusual care with credentials.

## Further reading

- [`docs/asmb8_info.md`](docs/asmb8_info.md),
  [`docs/asmb8_power.md`](docs/asmb8_power.md),
  [`docs/asmb8_boot.md`](docs/asmb8_boot.md),
  [`docs/asmb8_media.md`](docs/asmb8_media.md),
  [`docs/asmb8_redirection.md`](docs/asmb8_redirection.md) — per-module
  reference: options, return values, error classes, and examples.
- [`docs/capability-matrix.md`](docs/capability-matrix.md) — exactly what is
  verified against real firmware evidence, what is only unit/mock-tested, and
  what remains unproven, claim by claim.
- [`docs/protocol-notes.md`](docs/protocol-notes.md) — the normative iUSB/IVTP
  wire-format reference this implementation is built against, with
  provenance for every field.
- [`docs/testing.md`](docs/testing.md) — how to run everything, including the
  mock servers' fault-injection modes and the CI layout.
- [`docs/hardware-evidence-2026-08-08.md`](docs/hardware-evidence-2026-08-08.md)
  — the authoritative, dated record of what was actually observed on real
  hardware, including what is explicitly *not* proven yet.
- [`roles/asmb8_baremetal_install/README.md`](roles/asmb8_baremetal_install/README.md)
  — the end-to-end install role built on top of these modules.
- [`roles/asmb8_autoinstall_iso/README.md`](roles/asmb8_autoinstall_iso/README.md)
  and [`docs/proxmox-autoinstall.md`](docs/proxmox-autoinstall.md) — preparing
  a stock Proxmox VE ISO so it actually installs unattended, and the evidence
  behind why that preparation step is necessary at all.
- [SECURITY.md](SECURITY.md) — why this collection warrants unusual care with
  credentials.
- [CONTRIBUTING.md](CONTRIBUTING.md) — local verification sequence and
  tooling traps shared with the sibling collection.
