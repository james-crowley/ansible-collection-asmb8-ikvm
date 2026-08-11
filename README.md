<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Ansible Collection: `james_crowley.asmb8_ikvm`

<!-- Badges must stay on ONE line. GitHub renders the soft line break between two
     badge links as a <br>, which stacks them vertically instead of forming a row.
     The Galaxy badge reads the v3 published-collection index, not the v2 API:
     v2 no longer returns JSON for this path, so shields.io would render "resource
     not found" against it. Verified against the actual published collection.

     The CircleCI badge is STATIC, matching the sibling james_crowley.intel_amt
     collection, and for the same reason: a live dl.circleci.com badge 404s for
     anonymous visitors unless the project's "Free and Open Source" flag is on, and
     that flag makes build logs AND artifacts world-readable. This project's own
     hardware-evidence redaction (tests/hardware/redact-evidence.py) covers logs and
     playbook output, but store_artifacts content has not been audited to the same
     standard, so the flag stays off and the badge stays static rather than live. -->
[![Galaxy](https://img.shields.io/badge/dynamic/json?label=galaxy&query=%24.highest_version.version&url=https%3A%2F%2Fgalaxy.ansible.com%2Fapi%2Fv3%2Fplugin%2Fansible%2Fcontent%2Fpublished%2Fcollections%2Findex%2Fjames_crowley%2Fasmb8_ikvm%2F&color=blue)](https://galaxy.ansible.com/ui/repo/published/james_crowley/asmb8_ikvm/) [![CI: CircleCI](https://img.shields.io/badge/CI-CircleCI-343434?logo=circleci&logoColor=white)](https://app.circleci.com/pipelines/github/james-crowley/ansible-collection-asmb8-ikvm) [![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE) [![ansible-core](https://img.shields.io/badge/ansible--core-%3E%3D2.17-blue.svg)](https://docs.ansible.com/) [![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/) [![Status: pre-1.0](https://img.shields.io/badge/status-pre--1.0-yellow.svg)](#project-status)

Out-of-band management of **ASUS ASMB8-iKVM** baseboard management
controllers — AMI MegaRAC firmware on an ASPEED AST2400 — from Ansible: power
control and one-time boot selection over IPMI, plus this collection's
headline feature, a **native, pure-Python implementation of AMI's
proprietary iUSB virtual-media protocol**. It streams a local ISO straight
from the Ansible controller to the BMC's virtual CD-ROM, so a bare-metal host
can boot from it with **no PXE, DHCP, TFTP, NFS, or CIFS infrastructure**, and
**no Java runtime or JViewer applet** anywhere in the path.

The motivating use case is the same shape as PXE-based bare-metal
provisioning, without needing to stand up or maintain that infrastructure:
attach an installer ISO directly to a machine's virtual CD-ROM, arm a
one-time boot over IPMI, reset, and let it install. Then hand off to ordinary
Ansible roles once the OS is up.

## Why this exists

This board generation has **no Redfish** — that arrived with the
AST2500/ASMB9 family, one generation later than the AST2400/ASMB8 hardware
this collection targets. Standard IPMI, in turn, has no virtual-media
capability at all. Between those two facts there is no standard API for
attaching boot media to this board, which is the entire reason the protocol
work in this collection exists: AMI's **iUSB** protocol, layered on top of
the BMC's legacy `.asp` web-management session, is the only path to virtual
media this generation of hardware offers, and AMI has never published a
specification for it.

Power control and one-time boot selection are a different story: IPMI is
well understood and already correctly implemented by `pyghmi` (the same
library `community.general.ipmi_power`/`ipmi_boot` use internally). This
collection does not reinvent that path — `asmb8_power` and `asmb8_boot` are
thin, classified wrappers over it. All of the genuinely new work here is the
iUSB virtual-media client.

This collection is not the first tool to speak iUSB. Its implementation was
built by reverse engineering, informed by a third-party client for a related
AMI MegaRAC board and an independent Wireshark dissector, and — for the
facts those got wrong for this specific board — a local, non-redistributed
decompilation of the vendor's own JViewer client retrieved from the target
hardware. What is new: a pure-Python iUSB client, callable directly from an
Ansible module, with no external binary, no Java runtime, and no JViewer
applet anywhere in the path. See [NOTICE](NOTICE) for the full, per-file
provenance accounting, and [`docs/protocol-notes.md`](docs/protocol-notes.md)
for the normative wire format this implementation is built against.

## Requirements

- **Controller**: Python 3.10+ with `requests>=2.25.0` and `pyghmi>=1.5.0`.
  Install with `pip install -r requirements.txt`.
- **ansible-core >= 2.17.** The sanity-test boilerplate requirement changed
  incompatibly at 2.17, so no single module form is sanity-clean on both an
  older and a newer `ansible-core` — 2.17 is the floor rather than a choice.
- **Target**: nothing. No agent, no SSH, no Python interpreter on the managed
  node. The BMC is firmware, reachable independently of the host OS.
- An ASMB8-iKVM BMC that is **reachable and has a known admin credential**.
  This collection manages the BMC; it does not provision or factory-reset it.

## Installation

```bash
ansible-galaxy collection install james_crowley.asmb8_ikvm
pip install -r requirements.txt
```

To track `main` instead of a published release:

```bash
ansible-galaxy collection install git+https://github.com/james-crowley/ansible-collection-asmb8-ikvm.git
```

## Modules

| Module | Purpose | Mutates BMC? |
|---|---|---|
| [`asmb8_info`](docs/asmb8_info.md) | IPMI-observed capability/state facts, plus optional read-only `.asp` diagnostics | No |
| [`asmb8_power`](docs/asmb8_power.md) | Power control over IPMI, wrapping `pyghmi` directly | Yes |
| [`asmb8_boot`](docs/asmb8_boot.md) | One-time IPMI boot-device override; persistent changes are refused outright | Yes |
| [`asmb8_media`](docs/asmb8_media.md) | Stream a local ISO to the BMC's virtual CD-ROM over iUSB | Yes |
| [`asmb8_redirection`](docs/asmb8_redirection.md) | Report whether this BMC's own services (web, KVM, media, SSH, telnet) are enabled and reachable | No (`state` is accepted but always fails honestly — see [Known limitations](#known-limitations)) |
| [`asmb8_console`](docs/asmb8_console.md) | Open the iKVM console (IVTP) session headlessly: confirm the channel is live, or save one raw (undecoded) video frame | No |
| [`asmb8_reset`](docs/asmb8_reset.md) | Cold/warm-reset the BMC's management controller over IPMI — the recovery escape hatch for a wedged media session | Yes (BMC only; host power is unaffected) |
| [`asmb8_identify`](docs/asmb8_identify.md) | Turn the chassis identify LED on (for a bounded interval or indefinitely) or off, over standard IPMI | Yes (LED only) |
| [`asmb8_http_origin`](docs/asmb8_http_origin.md) | Run (or stop) an ephemeral, path-confined, lifetime-capped local HTTP file server, for installers that fetch bulk files over LAN-speed HTTP instead of the slower iUSB path | No (local process only; does not touch the BMC) |
| [`asmb8_bootstrap_image`](docs/asmb8_bootstrap_image.md) | Build a small bootable iPXE image carrying an embedded chain script, so iUSB only has to carry megabytes while the installer itself travels over HTTP | No (produces a local file; does not touch the BMC) |
| [`asmb8_ntp`](docs/asmb8_ntp.md) | Read and set the BMC's NTP servers and enable flag — the collection's only module that writes BMC configuration | **Yes** (writes `.asp` configuration) |

### Informational modules

These read the BMC's own `.asp` web-management interface. Every endpoint they
use is sourced from a capture of real hardware, checked in as fixtures under
`tests/unit/fixtures/asp/` — see [`docs/protocol-notes.md`](docs/protocol-notes.md).
**All of them are strictly read-only**: they issue `GET` requests and have no
`state` option, so none of them can change BMC configuration. Where a write
would be useful, each module's documentation describes what it would look like
and why it is deliberately absent.

| Module | Purpose | Mutates BMC? |
|---|---|---|
| [`asmb8_postcode`](docs/asmb8_postcode.md) | Read the BIOS POST code, optionally sampling it over a bounded window — the only out-of-band view of boot progress this board offers, since Serial-over-LAN does not work on it | No |
| [`asmb8_sel`](docs/asmb8_sel.md) | Read the System Event Log and its policy. The IPMI path is generally preferable; this exists for web-management-only reachability and cross-checking | No (clearing is deliberately not offered) |
| [`asmb8_sensors`](docs/asmb8_sensors.md) | Temperature, voltage and fan readings with decoded units. Discrete/event-only sensors report a null reading rather than a meaningless placeholder | No |
| [`asmb8_inventory`](docs/asmb8_inventory.md) | BMC firmware and auxiliary revision, device and product IDs, FRU area and platform feature list | No |
| [`asmb8_users`](docs/asmb8_users.md) | Configured accounts, their status and role groups. Unconfigured slots are reported as counts, not as accounts | No |
| [`asmb8_network`](docs/asmb8_network.md) | LAN channel, IP configuration, DNS and interface bonding | No |
| [`asmb8_sessions`](docs/asmb8_sessions.md) | Per-service state, plain and secure ports, timeouts and session limits | No |
| [`asmb8_alerts`](docs/asmb8_alerts.md) | Alerting configuration grouped by intent: where alerts go (SMTP, LAN destinations) and what fires them (event filters, policies, triggers) | No |
| [`asmb8_auditlog`](docs/asmb8_auditlog.md) | Audit log entries and the logging configuration | No (clearing is deliberately not offered) |

**Credential-shaped values are never returned.** Where the BMC exposes one —
an SMTP password, a DNS TSIG key, SSH key material, a user's email address —
these modules return a boolean such as `password_configured` instead of the
value. Field extraction is allow-list based, so a field on a firmware revision
this project has not captured is dropped rather than passed through. Audit log
entries are the one exception and are returned verbatim: sanitising free-text
log entries would corrupt the record and give false assurance, so they may
contain usernames or addresses and are documented as such.

All twenty are named in `meta/runtime.yml`'s `asmb8_ikvm` action group. Set
their shared connection options centrally with `module_defaults`:

```yaml
module_defaults:
  group/james_crowley.asmb8_ikvm.asmb8_ikvm:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
```

`asmb8_power`, `asmb8_boot`, and `asmb8_reset` accept every option in that
fragment (for `module_defaults` compatibility) but only actually use `host`,
`username`, `password`, and `ipmi_port` — the rest describe the `.asp`
web-management surface, which IPMI does not touch.

## Quickstart: unattended bare-metal install

Every module and option name below is real. Note `delegate_to: localhost` on
every task: an ASMB8-iKVM BMC is firmware and cannot execute a Python
payload, so every module in this collection runs on the **controller**,
never on the managed node. Replace the placeholder values with your own BMC
and target host.

```yaml
- name: Install an operating system onto bare metal via virtual media
  hosts: "{{ target }}"
  serial: 1                     # never fan out a reset
  gather_facts: false
  connection: local
  module_defaults:
    group/james_crowley.asmb8_ikvm.asmb8_ikvm:
      host: 192.0.2.10           # replace with your BMC's address
      username: "{{ asmb8_username }}"
      password: "{{ asmb8_password }}"
      tls_fingerprint: "{{ asmb8_tls_fingerprint }}"

  tasks:
    - name: Attach an already-prepared installer ISO to the virtual CD-ROM
      # This must be the output of `proxmox-auto-install-assistant prepare-iso`
      # (or the asmb8_autoinstall_iso role, which automates that step), never
      # a stock ISO -- see "A stock installer ISO will not install unattended"
      # below.
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
        host: 198.51.100.20       # replace with the target host's address
        port: 22
        timeout: 3600              # budget minutes, not seconds -- see below
        delay: 120

    - name: Detach the media
      james_crowley.asmb8_ikvm.asmb8_media:
        session_id: "{{ media.session_id }}"
        state: detached
      delegate_to: localhost
      no_log: true
```

The [`asmb8_baremetal_install`](roles/asmb8_baremetal_install/README.md) role
wraps this same sequence with a fan-out guard, an explicit destructive-action
confirmation, resumability across interrupted runs, and a guaranteed media
detach on failure. The
[`asmb8_autoinstall_iso`](roles/asmb8_autoinstall_iso/README.md) role
prepares the stock ISO this example depends on — see
[`docs/proxmox-autoinstall.md`](docs/proxmox-autoinstall.md) for why that
preparation step is necessary at all. **Neither role, nor the modules above,
have completed a full real OS install yet** — see
[Project status](#project-status) below for exactly how far this has been
taken.

## Transport and trust

The `.asp`/JNLP web-management plane (used by `asmb8_info`'s optional
`include_web_session` and by `asmb8_media`'s attach flow) defaults to HTTPS
on port 443 and requires an explicit trust decision:

- **`tls_fingerprint`** — SHA-256 leaf pinning. This is the trust mode most
  ASMB8-generation boards need in practice: their factory TLS certificate is
  typically self-signed, and some ship already expired, so chain validation
  cannot succeed against it regardless of `ca_path`.
- **`ca_path`** — ordinary chain and hostname verification, for a board whose
  factory certificate has been replaced with one issued by a real CA.
- **`allow_insecure_transport=true`** paired with `use_tls=false` — plaintext
  HTTP, never selected implicitly. The session cookie and the iUSB/KVM media
  token both cross the network recoverable by an on-path attacker when this
  is set; only use it on an isolated management VLAN.

IPMI (`asmb8_power`, `asmb8_boot`, `asmb8_reset`, and `asmb8_info`'s IPMI
facts) has no TLS layer of its own and is unaffected by any of the above —
it is plain UDP 623, using `pyghmi`'s defaults, with its own `ipmi_port`
option.

Some ASMB8-generation boards' TLS listeners offer a limited, non-forward-secret
ciphersuite that modern OpenSSL/Python builds exclude by default, which makes
a plain `requests.get(...)` fail the handshake outright while `curl` appears
to work — a difference in default cipher policy between HTTP clients, not a
bug. This collection's HTTP client re-enables the required cipher itself, so
this is transparent to a playbook. See
[`docs/hardware-evidence-2026-08-08.md`](docs/hardware-evidence-2026-08-08.md)
for the specifics observed on the board this was tested against.

## Virtual media

`asmb8_media` streams a local ISO from the controller to the BMC's virtual
CD-ROM over iUSB.

1. **It is always read-only.** The CD-ROM channel this collection speaks has
   no write opcode at all in the BMC's own firmware; there is no
   writable-image option to offer.
2. **A session is long-lived and the module call is not.** `state=attached`
   forks a detached background process that owns the connection and streams
   the ISO for as long as the install takes (potentially over an hour); the
   module invocation itself returns once that process reports `attached` or
   an early failure. `session_id` is how you refer to it again later —
   capture it if you will need to `state=detached` it.
3. **The virtual-media slot is single-occupancy, board-wide, with no
   timeout.** The BMC allows exactly one active media session for the entire
   board and has no server-side timeout to reclaim an abandoned one. Every
   `state=attached` call attempts to reclaim every *other* session this
   collection's own `runtime_dir` still has a record of against the same
   endpoint before opening a new connection — live or stale. That can only
   reclaim what it knows about; if nothing clears a wedged slot, `asmb8_reset`
   (a BMC-only cold/warm reset, host power unaffected) is the escape hatch.
4. **Budget minutes, not seconds.** Measured throughput on the one board this
   has been tested against is **≈790 KB/s** over the bulk-streaming phase —
   USB Mass Storage over this relay is strictly serial, one command
   outstanding at a time, roughly 32 KB per read at roughly 30 ms round trip.
   A 1.6 GB installer ISO is on the order of **35 minutes** of streaming
   alone. `TCP_NODELAY` was tried on the media socket as an obvious first fix
   and measured **no change** in a controlled A/B test — the bottleneck is
   structural, not a client-side tuning problem. See
   [`docs/netboot-design.md`](docs/netboot-design.md) for a design (not yet
   implemented) that routes the bulk of an installer's own payload over
   `asmb8_http_origin` and plain LAN-speed HTTP instead, using iUSB only for a
   small boot bootstrap.

`cd_port` (default 5120) is a *separate* TCP listener from `port` (the BMC's
HTTPS web-management port, default 443): `port` is used to log in and fetch
the JNLP document that mints a media session token; `cd_port` is where the
ISO bytes actually stream. A closed `cd_port` before any attach is normal —
it only binds once a JNLP fetch allocates a session.

### A stock installer ISO will not install unattended

Booting an unprepared installer ISO does not produce an unattended install.
A stock Proxmox VE ISO's own `grub.cfg` only sets a boot timeout inside a
conditional that is true only when `auto-installer-mode.toml` is present at
the ISO root — a stock ISO lacks that file, so its boot menu waits forever
with no timeout at all. The supported unattended path is an ISO already
prepared with `proxmox-auto-install-assistant prepare-iso` (which bakes in an
answer file and flips the ISO's own GRUB config to boot automatically); point
`asmb8_media`'s `image` option at the *output* of that command, never at a
stock ISO. The [`asmb8_autoinstall_iso`](roles/asmb8_autoinstall_iso/README.md)
role automates exactly that preparation step — see
[`docs/proxmox-autoinstall.md`](docs/proxmox-autoinstall.md) for the full
detail and the evidence behind it.

## Idempotence and check mode

| Module | Reads before writing | `changed` fires when | Check mode |
|---|---|---|---|
| `asmb8_info` | n/a (read-only) | never | Full read, identical to normal mode |
| `asmb8_power` | Yes (`get_power_state`) | Requested state differs from observed; always for `shutdown`/`reset`/`boot` | Reports the plan; never sends the IPMI command |
| `asmb8_boot` | Yes (`get_boot_device`) | Requested `device`/`uefi` differs from the current override | Reports the plan; never sends `set_bootdev()` |
| `asmb8_media` attach | Yes (session state file) | A new background process was actually forked | Validates the image and reports the plan; never forks or contacts the BMC |
| `asmb8_media` detach | Yes (session state file) | A live process was actually asked to stop | Reports whether a live session would be stopped; never signals it |
| `asmb8_reset` | No | Always, on a successful IPMI reset command | Reports the plan; never issues the reset |
| `asmb8_http_origin` | Yes (session state file) | A background server was actually started or stopped | Reports the plan; never forks or signals |

Two design commitments that hold across every module here:

- **An uncertain mutation is never retried automatically.** A timeout *after*
  a request was transmitted is reported with `indeterminate=true`, so a
  caller re-probes rather than blindly retrying a reset or attach that may
  have already taken effect.
- **One-shot boot is never silently re-armed persistently.** `asmb8_boot`
  refuses `persistent=true` outright, before any IPMI session is even opened.

## Error handling

Every failure carries a stable, machine-readable `error_class`:

`connection`, `tls_validation`, `authentication`, `unsupported_capability`,
`invalid_state`, `timeout`, `protocol`, `remote_operation`,
`identity_mismatch`, and `bmc_busy` — the one class this collection adds
beyond the taxonomy it shares with the sibling `james_crowley.intel_amt`
collection, because this board's web server can hang rather than refuse
under load, and because the single-occupancy media slot's rejection is a
distinct, nameable condition rather than an ordinary timeout.

Every user-visible message and diagnostic is redacted before it reaches a
module result: session cookies, login fields, iUSB/KVM tokens,
`Authorization`/`Cookie` headers, and generic `password=`/`token=`-shaped
values are all stripped, and diagnostics are length-bounded so a full HTTP
body never lands in a task result verbatim.

## Project status

**0.5.1 on Galaxy — pre-1.0, and not yet hardware-qualified.** That is not a
hedge; it is an accurate description of where this collection is today.

On one ASUS Z10PE-D16 WS / ASMB8-iKVM board (firmware 1.14, aux 1.14.2), this
collection has verified: the full iUSB authentication handshake byte-exact;
CD-ROM emulation and the real El Torito boot chain, matching `xorriso
-report_el_torito` on the same image; a bootloader streaming a real Proxmox
VE ISO across thousands of read requests; a media session surviving a host
power cycle; and IPMI power/boot-device control over `pyghmi` with true
one-time-boot semantics.

What is **not yet proven**, and must not be assumed:

- **A completed, unattended OS install, start to finish.** The furthest a
  real attempt has reached is the installer streaming its own files — one
  early run failed outright on virtual-CD I/O errors before a fix landed; a
  later run streamed well past that point but was not carried to completion.
  No install has finished on any module or on the `asmb8_baremetal_install`
  role.
- **Whether a booted guest OS can obtain its own media session.** The BMC's
  media slot allows exactly one session with no server-side timeout; if this
  collection's own process is still holding it when the guest OS
  re-enumerates USB storage, the guest may be unable to reach its own media —
  untested.
- **KVM video decoding.** The IVTP handshake is understood and exercised;
  no console frame has ever been decoded from this board, and
  `asmb8_console`'s `capture=decoded_frame` deliberately fails with
  `error_class=unsupported_capability` rather than approximating a decode.
- **Any board other than the one tested.** One machine, one firmware version.
  This is repeatability at best, not a compatibility guarantee.

See [`docs/hardware-evidence-2026-08-08.md`](docs/hardware-evidence-2026-08-08.md)
for the full, dated, falsifiable record everything above is drawn from, and
[`docs/capability-matrix.md`](docs/capability-matrix.md) for the complete
claim-by-claim accounting — including exactly what rests on real firmware
evidence, what rests only on reading someone else's source, and what rests
on nothing yet.

## Known limitations

Each of these is a measured or reasoned limit, not a hedge. Full detail and
sourcing live in the linked docs; this is the skimmable version.

- **IPMI Serial-over-LAN does not work on the tested board.** A SOL session
  opens cleanly via `pyghmi`, the channel-level SOL payload was enabled,
  per-user SOL access was already granted, and both plausible bitrates were
  tried — three configurations, zero bytes received in every case. This
  collection does not depend on SOL for anything; media-channel read patterns
  were used instead to diagnose installer behaviour. See
  [`docs/hardware-evidence-2026-08-08.md`](docs/hardware-evidence-2026-08-08.md)
  ("Serial-over-LAN") for what was tried.
- **`asmb8_console` has the least evidence of any module here.** It completes
  a real IVTP handshake and can save one raw, still-encoded video frame, but
  has **zero** live-hardware evidence and **zero** unit/mock coverage of its
  handshake state machine — everything it does is sourced from decompiled
  vendor-client analysis alone, including at least one wire-format detail
  flagged in its own source as unverified. `capture=decoded_frame` fails
  outright with `error_class=unsupported_capability`; this collection
  implements no part of the AMI/ASPEED video codec and does not plan to fake
  one. See [`docs/asmb8_console.md`](docs/asmb8_console.md).
- **`asmb8_redirection` can report service state but never toggle it.** No
  sourced RPC exists on this BMC's `.asp` surface for toggling whether a
  service is enabled, so passing `state` always fails with
  `error_class=unsupported_capability` rather than mutating through a guessed
  endpoint — deliberately, and matching the sibling `james_crowley.intel_amt`
  collection's `amt_redirection` module. Its `known`/`enabled` signals come
  from a static catalog read from the BMC's web UI once, not a live query;
  only its `reachable` signal is a genuine live probe. See
  [`docs/asmb8_redirection.md`](docs/asmb8_redirection.md).
- **Throughput is slow, and client-side tuning does not help.** See
  [Virtual media](#virtual-media) above — budget minutes, not seconds, and do
  not expect `TCP_NODELAY` or similar socket tuning to change that; it was
  tried and measured to make no difference.
- **The factory TLS certificate cannot be chain-validated on some boards.**
  Fingerprint pinning (`tls_fingerprint`) is the trust mode to use unless a
  board's certificate has been replaced. See [Transport and trust](#transport-and-trust).
- **Never trust a BMC-supplied timestamp.** The tested board's own clock read
  nearly a decade off from actual time. That is relevant beyond cosmetics: it
  is also the BMC's own view of whether its (already-expired) certificate is
  currently valid, so a BMC-reported "certificate OK" is not to be trusted
  either.
- **The virtual-media slot is single-occupancy, board-wide, with no
  timeout.** See point 3 under [Virtual media](#virtual-media).

See [`docs/roadmap.md`](docs/roadmap.md) for capability gaps that are tracked
but not yet built (network-boot design work, additional device classes, and
more).

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
the full policy, and how to report a vulnerability privately. In brief:

- `password` is `no_log` in every module's argument spec; every example in
  this repository also sets task-level `no_log: true` as defence in depth.
- Credentials, session cookies, and iUSB/KVM tokens are never written to
  operation receipts, facts, or the `asmb8_media` session state file.
- Fingerprint pinning is the only trust mode that works against a factory
  certificate that cannot be chain-validated; plaintext transport requires an
  explicit, never-implicit opt-in.
- The iUSB protocol's own authentication and confidentiality properties are
  not independently verified beyond what riding on top of the `.asp` web
  session provides.

## License and attribution

GPL-3.0-or-later. See [LICENSE](LICENSE).

This collection's iUSB implementation draws on a third-party MIT-licensed
client for a related AMI MegaRAC board, an independent Wireshark dissector,
and a local, non-redistributed decompilation of the vendor's own JViewer
client retrieved from the target hardware, used solely to understand an
undocumented protocol well enough to interoperate with it. **Neither the
vendor binaries nor any decompiled output is redistributed in this
collection.** Full per-file provenance — which sources were consulted, what
was taken from each, and every point where they disagreed and which one won —
is in [NOTICE](NOTICE).

## Contributing

Issues and PRs welcome, including issues that are just "here is hardware
evidence" or "here is a packet capture" — see the feature-request template.
Conventional commits; every user-facing change needs a changelog fragment in
`changelogs/fragments/`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the local verification sequence
and the practical tooling traps this project shares with the sibling
`james_crowley.intel_amt` collection, and [SECURITY.md](SECURITY.md) for why
this collection warrants unusual care with credentials.

## Documentation

- [`docs/asmb8_info.md`](docs/asmb8_info.md),
  [`docs/asmb8_power.md`](docs/asmb8_power.md),
  [`docs/asmb8_boot.md`](docs/asmb8_boot.md),
  [`docs/asmb8_media.md`](docs/asmb8_media.md),
  [`docs/asmb8_redirection.md`](docs/asmb8_redirection.md),
  [`docs/asmb8_console.md`](docs/asmb8_console.md),
  [`docs/asmb8_reset.md`](docs/asmb8_reset.md),
  [`docs/asmb8_http_origin.md`](docs/asmb8_http_origin.md) — per-module
  reference: options, return values, error classes, and examples.
- [`docs/capability-matrix.md`](docs/capability-matrix.md) — exactly what is
  verified against real firmware evidence, what is only unit/mock-tested, and
  what remains unproven, claim by claim.
- [`docs/protocol-notes.md`](docs/protocol-notes.md) — the normative
  iUSB/IVTP wire-format reference this implementation is built against, with
  provenance for every field.
- [`docs/hardware-evidence-2026-08-08.md`](docs/hardware-evidence-2026-08-08.md)
  — the authoritative, dated record of what was actually observed on real
  hardware, including what is explicitly not proven yet.
- [`docs/testing.md`](docs/testing.md) — how to run everything, including the
  mock servers' fault-injection modes and the CI layout.
- [`docs/proxmox-autoinstall.md`](docs/proxmox-autoinstall.md) — preparing a
  stock Proxmox VE ISO so it actually installs unattended, and the evidence
  behind why that preparation step is necessary at all.
- [`docs/netboot-design.md`](docs/netboot-design.md) — design research (not
  yet implemented) for routing the bulk of an install over LAN-speed HTTP via
  `asmb8_http_origin` instead of the slower native iUSB path.
- [`docs/roadmap.md`](docs/roadmap.md) — capability gaps and what is planned
  next, evidenced to the same standard as the rest of this documentation set.
- [`roles/asmb8_baremetal_install/README.md`](roles/asmb8_baremetal_install/README.md)
  — the end-to-end install role built on top of these modules.
- [`roles/asmb8_autoinstall_iso/README.md`](roles/asmb8_autoinstall_iso/README.md)
  — preparing a stock Proxmox VE ISO for genuinely unattended installation.
- [SECURITY.md](SECURITY.md) — how to report a vulnerability, and why this
  collection warrants unusual care with credentials.
- [CONTRIBUTING.md](CONTRIBUTING.md) — local verification sequence and
  tooling traps shared with the sibling collection.
