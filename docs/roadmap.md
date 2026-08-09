<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Roadmap: capability gaps and what to build next

This is desk research: no network request was made to any BMC while writing
it. Everything about "what we have" is traced to a file and line in this
repository; everything about "what a MegaRAC BMC on this silicon can do" is
either a standard IPMI command (cited by netfn/command), something read
directly out of `pyghmi`'s installed source (a controller-side dependency
this collection already ships with, inspected the same way
`plugins/module_utils/ipmi.py`'s own docstring says it was — reading source,
never touching a BMC), or an external source with a citation. Where a claim
is an inference rather than something a source states outright, it is marked
as such.

Companion documents: [`docs/capability-matrix.md`](capability-matrix.md) (the
tiered evidence model this roadmap borrows), and
[`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md) (the
dated hardware record). This file does not repeat their content; it extends
the same standard of evidence forward into "what's not built yet."

## Two constraints that shape every recommendation below

**IPMI Serial-over-LAN does not work on this board.** The channel-level SOL
payload was disabled, then enabled; per-user SOL access was already granted;
both plausible bitrates were tried; three configurations were exercised.
Zero bytes arrived across repeated resets
(`docs/hardware-evidence-2026-08-08.md`, "Serial-over-LAN: configured
correctly and still silent"). Nothing below proposes a feature that depends
on a working serial console. Any capability that would normally lean on SOL
for observability (for example, watching an OS boot without KVM) is marked
**blocked-by-SOL** and is not on this roadmap.

**`asmb8_redirection` reports three independent signals on purpose, and
`state` fails on purpose.** `known`/`enabled`/`reachable` are deliberately
never collapsed into one boolean
(`plugins/modules/asmb8_redirection.py:20-52`), and `state` always raises
`unsupported_capability` before any network is touched
(`plugins/modules/asmb8_redirection.py:411-422`) because no sourced RPC exists
on this BMC's `.asp` surface for toggling a service's enablement. This is not
a stub waiting to be filled in lightly — see "Not achievable / needs new
discovery" below for what filling it in honestly would actually require.

---

## 1. What we have today, and how proven it is

Read directly from `plugins/modules/` and `plugins/module_utils/`, then
cross-checked against `docs/hardware-evidence-2026-08-08.md` and
`docs/capability-matrix.md`.

| Module / capability | What it actually does | Proof tier |
|---|---|---|
| `asmb8_power` (`plugins/modules/asmb8_power.py`) | IPMI chassis power control (`on`/`off`/`shutdown`/`reset`/`boot`) via `pyghmi.ipmi.command.Command.get_power()`/`set_power()`, wrapped in `plugins/module_utils/ipmi.py:201-239` | **Proven on hardware.** `get_power()` returned `{'powerstate': 'on'}` live (`hardware-evidence...md` §"IPMI") |
| `asmb8_boot` (`plugins/modules/asmb8_boot.py`) | One-time IPMI boot-device override via `get_bootdev()`/`set_bootdev()`; refuses `persistent=true` before opening a connection (`asmb8_boot.py:261-269`) | **Proven on hardware.** `set_bootdev('cd', persist=False)` reverted to `default` after reset — true one-time semantics confirmed live |
| `asmb8_info` — IPMI facts (`power_state`, `boot_device`, `mc_info`) | Thin, per-field-degrading reads over the same IPMI path | **Proven on hardware** for all three fields |
| `asmb8_info` — `include_web_session` | Optional `.asp` login + raw, unparsed `hoststatus.asp` text | **Implemented but weakly proven.** `proven` in its own `capabilities` dict is only `true` for the run that actually logged in (`asmb8_info.py:425-433`); `hoststatus.asp`'s response shape is explicitly undocumented (`asp.py:697-705`) |
| `asmb8_info` — `capabilities.virtual_media`/`remote_console` | Reports `supported=null` (unknown, not false) | **Explicitly marked unproven by the module itself** (`asmb8_info.py:434-443`) — notable because `asmb8_media`/`asmb8_console` elsewhere in this same collection *do* work; this field is conservative on purpose |
| `asmb8_info` — `capabilities.redfish` | Reports `supported=false, proven=true` | **Stated as a hardware-generation fact, not a live probe** (`asmb8_info.py:444-448`) — see §3's Redfish discussion below for exactly how confident that claim actually is |
| `asmb8_media` | Full iUSB virtual-CD client: auth handshake, `TEST UNIT READY`/`READ CAPACITY`/`READ(10)`/`READ(12)`/`READ TOC`, background-daemon session model, single-slot reclamation | **Proven on hardware, extensively.** El Torito boot chain matched byte-for-byte against `xorriso`, 2,839 real read requests served, a real defect (`READ TOC` ignoring CDB allocation length) found and fixed against live traffic. This is the flagship, most-proven capability in the collection |
| `asmb8_redirection` — `known`/`enabled`/`capacity` | Static catalog built from the BMC's own Services web page, read by a human, not observed on the wire (`asmb8_redirection.py:304-318`) | **Vendor self-report, not independently confirmed** for `enabled`/timeout/max-sessions figures. Port numbers and the plaintext/secure split *are* wire-confirmed |
| `asmb8_redirection` — `reachable` | Live, this-run TCP connect-and-close, every run | **Proven, but trivially so** — this is a bare socket probe, not a protocol claim |
| `asmb8_redirection` — `state` (enable/disable a service) | Always raises `unsupported_capability` before touching the network (`asmb8_redirection.py:411-422`) | **Deliberately stubbed.** No sourced RPC exists; this is a documented refusal, not a bug |
| `asmb8_console` | Full IVTP handshake (`GET_WEB_TOKEN`/`VALIDATE_VIDEO_SESSION`/`RESUME_REDIRECTION`) plus `capture=raw_frame` (saves one undecoded video frame) | **Implemented, essentially unproven.** Per `docs/capability-matrix.md` Tier 4: "zero live-hardware evidence and zero unit/mock test coverage of the handshake state machine — the only module and `module_utils` file in this collection with neither." Every wire-format fact behind it is Tier 1 (decompiled-client analysis) with nothing at Tier 2 or 3 |
| `asmb8_console` — `capture=decoded_frame` | Always raises `unsupported_capability` before any network access (`asmb8_console.py:536-544`) | **Deliberately stubbed.** Decoding AMI/ASPEED's VQ+JPEG(DCT) codec is out of scope; refusing honestly beats a fake decode |
| `module_utils/asp.py` — `get_host_status()`/`set_power()` (`hostctl.asp`) | Plumbing that POSTs/GETs two endpoints whose request/response shapes are explicitly **not sourced from any capture** (`asp.py:686-696`) | **Unused, unsourced, and not wired into any module.** `asmb8_power` correctly uses IPMI instead. This is dead-but-honest plumbing, not a gap — IPMI already does power control reliably, so nothing should be built on this path |
| `module_utils/ipmi.py` (`IpmiClient`) | Wraps exactly three `pyghmi` calls: `get_power`/`set_power`, `get_bootdev`/`set_bootdev`, `get_mci` | This is the headline finding of this whole roadmap: `pyghmi` (already a required dependency, `requirements.txt`) implements far more than these three calls — see §2 |

**One structural observation that motivates most of this roadmap:** this
collection's own `errors.py` docstring says the design goal is "never assert
a protocol fact it cannot source." For IPMI, sourcing is nearly free —
`pyghmi` is already a required dependency, already does the RMCP+ session
handshake, and already implements a large surface of standard IPMI commands
that nothing in this collection currently calls. The gap tier below is
therefore split sharply into "cheap because `pyghmi` already speaks it" versus
"expensive because it requires the same kind of `.asp`/decompiled-client
reverse engineering that `asmb8_media` needed."

---

## 2. What `pyghmi` already implements and this collection does not yet expose

Read directly from the installed dependency's source
(`.venv/lib/python3.13/site-packages/pyghmi/ipmi/command.py`, version
1.6.19 as staged in this repo's `.venv`) — the same sourcing method
`plugins/module_utils/ipmi.py`'s own docstring already uses for the three
calls it does wrap. No BMC was contacted to produce this list; it is a
straight reading of a Python library already sitting in this repository's
dependency tree.

`pyghmi.ipmi.oem.lookup.get_oem_handler()` (`pyghmi/ipmi/oem/lookup.py:27-31`)
maps only three IANA manufacturer IDs (20301/19046/7154, all Lenovo/IBM) to a
vendor-specific OEM handler. **American Megatrends is not in that map**, so
every `pyghmi` call on this board falls back to
`pyghmi.ipmi.oem.generic.OEMHandler`. That fallback class matters a lot for
this roadmap:

- Some of its methods fall through to a **real, standard IPMI command**
  when the generic OEM has nothing to say — `set_identify()`
  (`command.py:555-594`) is the clearest example: it tries the OEM path,
  catches `UnsupportedFunctionality`, and falls back to the standard IPMI
  Chassis Identify command (netfn `0x00`, command `0x04`). This works on any
  compliant BMC, this one included, generically.
- Some of its methods implement a **standard command directly in the generic
  class itself**, no OEM branching involved — `get_system_power_watts()`
  (`pyghmi/ipmi/oem/generic.py:48-52`) issues DCMI's Get Power Reading
  (netfn `0x2c`, command `0x02`, group extension byte `0xdc`) unconditionally.
  Whether it *returns something meaningful on this specific board* depends on
  whether this 2014 firmware is DCMI-compliant, which is unconfirmed — see
  §5's open questions.
- Some of its methods are **pure no-ops on generic hardware** —
  `get_ntp_enabled()`/`get_ntp_servers()`/`get_leds()`
  (`pyghmi/ipmi/oem/generic.py:235-254,227-233`) return `None`/`()`
  unconditionally, and `update_firmware()`/`get_update_status()`
  (`generic.py:347-352`) raise `UnsupportedFunctionality` outright. These are
  not gaps this collection can close by writing more Ansible code — `pyghmi`
  itself has nothing to call for a generic AMI board here.

The calls below are **not OEM-gated** — they use standard IPMI/DCMI netfn/cmd
pairs directly against the BMC, the same way `get_power()`/`get_bootdev()`
already do, and are the cheapest real capability gap this collection has:

| `pyghmi` call | Standard command | Source |
|---|---|---|
| `get_sensor_data()` / `get_sensor_descriptions()` | Get Sensor Reading, netfn `0x04` cmd `0x2d`, driven off the standard SDR repository | `command.py:1092-1130` |
| `get_event_log(clear=)` | Get SEL Info / Get SEL Entry / Clear SEL (`pyghmi.ipmi.sel.EventHandler`) | `command.py:622-637` |
| `get_inventory()` / `get_inventory_of_component()` | Get FRU Inventory Area Info (netfn `0x0a` cmd `0x10`) + Read FRU Data (cmd `0x11`) | `command.py:723-747` |
| `_get_device_id()` (private; BMC firmware rev, device/product/mfg IDs) | Get Device ID, netfn `0x06` cmd `0x01` | `command.py:234-250` |
| `get_net_configuration()` / `set_net_configuration()` | Get/Set LAN Configuration Parameters, netfn `0x0c` cmd `0x02`/`0x01` | `command.py:945-1091` |
| `get_users()` / `get_user_access()` / `set_user_access()` / `set_user_name()` / `set_user_password()` / `create_user()` / `user_delete()` | User/Channel Access commands, netfn `0x06` | `command.py:1742-2124` |
| `get_alert_destination()` / `set_alert_destination()` / `set_alert_community()` | PEF LAN Alert Destination config, netfn `0x0c` cmd `0x02` (generic-class implementation, not OEM-gated — `generic.py` has no override) | `command.py:1176-1387` |
| `reset_bmc()` | Cold Reset, netfn `0x06` cmd `0x02` | `command.py:409-413` |
| `get_system_power_watts()` | DCMI Get Power Reading, netfn `0x2c` cmd `0x02` | `pyghmi/ipmi/oem/generic.py:48-52` |
| `set_identify()` | Chassis Identify (fallback path only), netfn `0x00` cmd `0x04` | `command.py:555-594` |

None of this required a single byte on the wire to establish — it is a
library already vendored into this project's dependency tree, read the same
way this collection already reads `pyghmi` for the three calls it uses today.

---

## 3. Everything else a MegaRAC BMC on this generation can plausibly do

Organized by the capability list in the brief. For each: which transport
would serve it, how confident that is, and what (if anything) is already
sourced in this collection's own code.

### Sensor and thermal readings
**IPMI, high confidence, standard.** Get Sensor Reading (netfn `0x04` cmd
`0x2d`) against the standard SDR repository. `pyghmi.get_sensor_data()`
already implements this generically (§2). No `.asp` or Redfish path needed
or better.

### Fan control/policy (set a fan mode, not just read RPM)
**No standard IPMI command for a fan "policy" (quiet/performance/full-speed)
exists** — reading a fan's RPM is a sensor read (above); actually changing
its control curve is universally vendor-specific. AMI generic MegaRAC boards
of this era typically expose this only through their web UI's Fan Profile
page. No `.asp` endpoint for it is sourced anywhere in this collection
(`plugins/module_utils/asp.py` documents only `create.asp`,
`getsessiontoken.asp`, `jviewer.jnlp`, and the two unsourced
`hoststatus.asp`/`hostctl.asp` TODOs). **This is an inference, not a sourced
fact** — it is possible this board exposes fan policy via a raw OEM IPMI
command this desk review has no way to discover without a wire capture.

### Power consumption metering
**IPMI/DCMI, medium confidence.** `get_system_power_watts()` issues the
standard DCMI Get Power Reading command unconditionally (§2), so it is cheap
to try, but whether this firmware actually answers meaningfully is
unconfirmed — DCMI compliance is a separate, optional certification from
base IPMI, and this collection has no evidence either way for this
board. See §5.

### Chassis/power state and boot device order
**Already implemented** (`asmb8_power`, `asmb8_boot`), proven on hardware.

### SEL (system event log) read and clear
**IPMI, high confidence, standard.** Get SEL Info/Entry/Clear SEL, fully
wrapped by `pyghmi.get_event_log(clear=)` (§2). This is the single most
valuable gap on this list given SOL does not work — see §4.

### FRU inventory
**IPMI, high confidence, standard.** Get FRU Inventory Area Info + Read FRU
Data, wrapped by `pyghmi.get_inventory()` (§2).

### BIOS/BMC firmware version reporting
**Split.** BMC firmware revision and device/product/manufacturer IDs: IPMI
standard, Get Device ID (netfn `0x06` cmd `0x01`), already read internally by
`pyghmi._get_device_id()` (§2) but not currently surfaced by
`asmb8_info`. **BIOS version specifically is a different question** — IPMI
has no standard command for host BIOS version; it is SMBIOS/DMI data that
normally lives on the host OS side, not the BMC. Some vendors expose a BIOS
version string via an OEM FRU field or a proprietary web page, but nothing
here is sourced for that. Treat "BMC firmware version" as cheap and "BIOS
version" as **not achievable via any transport this collection has sourced.**

### BMC firmware update
**No safe path.** `pyghmi.update_firmware()` raises
`UnsupportedFunctionality` for a generic (non-Lenovo) OEM (§2) — there is no
portable, standard-IPMI firmware-update mechanism this library can fall back
to the way it does for Chassis Identify. AMI MegaRAC boards of this era
generally update firmware through a vendor web-UI upload, and no such `.asp`
endpoint is sourced anywhere in this collection. Combined with the explicit
brick risk called out in the brief, this belongs in "not achievable without
new discovery," and even with discovery it warrants a much higher confidence
bar than anything else on this list before it is attempted at all.

### BIOS settings
**No known transport.** No Redfish (see below), no sourced `.asp` endpoint,
and IPMI has no standard mechanism for reading or writing arbitrary BIOS
setup options (IPMI's own "Boot Options" parameters cover boot order, which
`asmb8_boot` already handles — not general BIOS settings). **Not achievable
on this hardware without new discovery this desk review has no path to
perform.**

### User and privilege management
**IPMI, high confidence, standard** — Set/Get User Access, Set/Get User Name,
Set User Password, all netfn `0x06`, fully wrapped by `pyghmi` (§2). This is
exactly the kind of capability the brief calls out explicitly: **read-only
(`get_users`/`get_user_access`) is safe and belongs in an early tier; any
write path (create/delete/change password) can lock out access if done
wrong and belongs in a much later tier regardless of how cheap it is to
build.**

### Network configuration (IP/VLAN/hostname)
**IPMI, high confidence for IP/VLAN/gateway, standard** — Set/Get LAN
Configuration Parameters, netfn `0x0c`, fully wrapped by `pyghmi` (§2).
Hostname is weaker: `pyghmi.get_hostname()` falls back to the DCMI
Management Controller ID field (`get_mci()`) for a generic OEM
(`command.py:1387-1403`) — the same string `asmb8_info` already reports as
`mc_info`, so a dedicated hostname read would not add anything new here.
Per the brief's own risk framing: **a write to this BMC's own management IP
can sever the very connection making the change.** Read-only reporting is
safe and cheap; write belongs in the same later, higher-confidence tier as
user management.

### NTP/time
**No standard IPMI path for a generic OEM.** `pyghmi.get_ntp_enabled()`/
`get_ntp_servers()`/`set_ntp_server()` all delegate purely to the OEM layer
and the generic fallback returns `None`/`()` unconditionally
(`pyghmi/ipmi/oem/generic.py:235-264`) — this is not a gap in this
collection's own code, it is `pyghmi` itself demonstrating there is no
standard command to fall back to. Any NTP configuration on this board is a
web-UI-only, unsourced `.asp` feature. **Not achievable without new
discovery.**

### SNMP and alerting/SMTP destinations
**Split.** SNMP/PET trap destination configuration: IPMI standard, PEF LAN
Alert Destination config (netfn `0x0c` cmd `0x02`), implemented directly in
`pyghmi`'s generic class, not OEM-gated (§2) — genuinely cheap and standard.
**SMTP/email alerting has no standard IPMI equivalent** — where AMI boards
support it at all, it is a proprietary web-config page; no `.asp` endpoint
for it is sourced here. Low relevance to this collection's stated purpose
either way (a one-shot headless install does not need standing alert
infrastructure).

### Syslog
**No standard IPMI mechanism.** Remote syslog forwarding configuration, where
it exists on AMI boards, is a web-UI-only feature. No endpoint sourced.
**Needs new discovery**, and even then is orthogonal to this collection's
core purpose.

### LDAP/AD integration
**No standard IPMI mechanism** for a generic OEM. This is an authentication
backend change with real lockout risk if done wrong, no sourced endpoint,
and low relevance to the stated purpose. **Do not pursue** without a
concrete, sourced reason to.

### Certificate management
**No IPMI mechanism at all** — the TLS certificate belongs to the `.asp`
HTTPS listener only; IPMI has no TLS layer (`plugins/module_utils/ipmi.py`'s
own docstring and `README.md`'s "Transport and trust" section both confirm
IPMI is unaffected by any of this). Any certificate-replacement capability
would need an unsourced `.asp` upload endpoint, and getting it wrong risks
breaking the *only* currently-working trust path (fingerprint pinning against
the existing, if expired, factory cert) with no way back short of physical
access. **Needs new discovery; high risk regardless of appeal.**

### Service enable/disable and port configuration
**Read side already implemented** (`asmb8_redirection`). **Write side is
specifically not achievable without new discovery** — this was investigated
directly for `asmb8_redirection` and no RPC was found
(`plugins/module_utils/asp.py` documents every endpoint this collection has
sourced; none of them toggle a service). The cheapest next step is a browser
capture of the BMC's own Services admin page actually flipping a toggle —
manual, hardware-adjacent work this desk review cannot perform.

### KVM/media encryption toggles
**No sourced RPC.** `kvm_secure`/`vm_secure` are properties of *which scheme
was used to fetch the JNLP*, not a standing, independently configurable board
setting (`plugins/module_utils/models.py:149-157`, confirmed live: the same
board returned `-kvmsecure 0` over HTTP and `-kvmsecure 1` over HTTPS minutes
apart). Whatever configuration option genuinely disables/enables encryption
board-wide (referenced obliquely in `asmb8_console.py:96-98`'s note that
"media/KVM encryption is disabled in this BMC's configuration") has no
sourced `.asp` endpoint. Low priority: the working iUSB/KVM path today is
plaintext, and changing this configuration risks breaking that path with no
confirmed way back.

### Session listing and termination
**Split, and the useful half is not the IPMI half.** IPMI has a standard Get
Session Info command (netfn `0x06` cmd `0x3d`) for active *IPMI* sessions —
cheap and standard, but low value, because the single-occupancy resource this
collection actually cares about (the `.asp`/iUSB media/KVM session) is not an
IPMI session at all. No sourced `.asp` endpoint lists or terminates a web/KVM
session by ID; `asmb8_media`'s own reclamation logic works around this by
tracking sessions *this collection itself* opened, not by querying the BMC
for what is currently held (`plugins/module_utils/media_session.py`,
referenced in `README.md`'s "single-occupancy" limitation). **Not achievable
for the resource that actually matters, without new discovery.**

### BMC cold/warm reset
**IPMI, high confidence, standard, and already the documented recovery
procedure.** Cold Reset (netfn `0x06` cmd `0x02`) is exactly what `README.md`
and `docs/hardware-evidence-2026-08-08.md` already point operators at as the
manual escape hatch for a wedged media slot ("a BMC cold reset ... is the
operator's escape hatch when nothing else clears it — it resets the
management controller only, not host power"). `pyghmi.reset_bmc()` wraps this
directly (§2). This capability exists today only as a sentence in the docs
telling a human to run `ipmitool mc reset cold` by hand — see §4.

### Configuration backup and restore
**No standard IPMI mechanism; no sourced `.asp` endpoint.** Where AMI
firmware supports this, it is a web-UI export/import feature. Restore
specifically carries real risk (a bad import could touch network/user
config). **Not achievable without new discovery**, and restore in particular
would need a much higher confidence bar than this desk review can currently
supply.

### Hardware inventory/DMI
**FRU inventory (above) is the IPMI-native equivalent** and is cheap. True
SMBIOS/DMI data lives on the host OS (`dmidecode`), not the BMC, and is not
generally IPMI-reachable at all outside vendor-specific OEM extensions this
collection has no source for on this board. Treat "hardware inventory via
FRU" as the achievable version of this ask, not literal DMI.

### Redfish
**The repository's own claim** (`plugins/modules/asmb8_info.py:444-448`,
`README.md`'s "Why this exists," `docs/hardware-evidence-2026-08-08.md` line
16) is that this generation predates Redfish entirely, which arrived with
AST2500/ASMB9, and that this is "a confirmed hardware-generation fact, not a
live probe." Web research corroborates this **directionally but not with a
primary ASPEED/AMI statement naming AST2400 specifically**: AST2500 is
documented as a genuinely different hardware generation from AST2400 (ARM11
vs. ARM9 core, PCIe Gen2, eSPI — see MiTAC's own AST2500 configuration guide
and Redfish API document), and Redfish tooling/documentation for AMI
MegaRAC-family boards clusters around the AST2500 generation. No source
found in this research directly states "AST2400 MegaRAC firmware never
implements Redfish, no exceptions" — some vendors have, in principle,
backported partial Redfish shims onto older silicon, though none is known or
suspected for this specific board or firmware line. **Verdict: the
repository's claim is well-supported and should keep being treated as true
for planning purposes, but it is not independently confirmed by a primary
ASPEED/AMI source for this exact board+firmware combination** — see §5 for
the cheap, safe way to close this gap for real once hardware access resumes.

---

## 4. Prioritized roadmap

Ranked by value to the stated purpose (headless bare-metal install, no
PXE/DHCP/TFTP/NFS/CIFS), cost, testability without hardware, and risk. Per
the brief: **anything that can brick a board or lock out access is pushed to
a later tier regardless of how appealing or cheap it looks**, and read-only
capabilities are sequenced ahead of the write versions they'd unblock.

### Tier 0 — already have (for contrast, not proposed)

`asmb8_power`, `asmb8_boot`, `asmb8_info`, `asmb8_media`, `asmb8_redirection`
(read side), `asmb8_console` (handshake-only). See §1.

### Tier 1 — build first: standard IPMI, `pyghmi` already speaks it, low risk, read-heavy

| # | Module | Transport | Returns/changes | Effort | Needs hardware to develop? | Discover first? |
|---|---|---|---|---|---|---|
| 1 | `asmb8_reset` | IPMI, netfn `0x06` cmd `0x02`/`0x03` (Cold/Warm Reset) via `pyghmi.reset_bmc()` | Resets the BMC's management controller only, not host power. Promotes the documented manual `ipmitool mc reset cold` recovery step (README, hardware-evidence doc) to a first-class, testable module | S | No — mirrors `asmb8_boot`'s exact shape; unit-testable by extending the existing `FakeIpmiCommand` test double (`tests/integration/mock_servers/ipmi_server.py`) with a `reset_bmc()` fake, the same way `get_power`/`set_power` are already faked there | Nothing — `reset_bmc()` is already proven in the sibling `pyghmi` library's own source |
| 2 | `asmb8_sel` | IPMI, netfn `0x0a` (Get SEL Info/Entry, Clear SEL) via `pyghmi.get_event_log(clear=)` | Read: SEL entries as an iterable. Optional `clear=true`: same atomic fetch-and-clear `pyghmi` already offers, so nothing is lost between read and clear | S/M | No — same test-double pattern as above | Nothing for the read path. For `clear`, confirm on hardware once available that this board's SEL actually populates meaningfully (a board that never wrote an entry would make the feature look broken when it's just untested, not wrong) |
| 3 | `asmb8_sensors` | IPMI, netfn `0x04` cmd `0x2d` (Get Sensor Reading) + SDR, via `pyghmi.get_sensor_data()`/`get_sensor_descriptions()`; best-effort DCMI Get Power Reading via `get_system_power_watts()` | Thermal/fan/voltage sensor readings by name and type; power-consumption wattage if this firmware turns out to be DCMI-compliant | S/M | No for the sensor path (same pattern). The DCMI power-reading half needs one hardware probe to learn whether this firmware answers it at all (see §5) — build it, but don't promise it works until that's confirmed | DCMI compliance for power reading specifically (§5); sensor reads need nothing |
| 4 | `asmb8_inventory` | IPMI, netfn `0x0a` (FRU Inventory Area Info + Read FRU Data) via `pyghmi.get_inventory()`; netfn `0x06` cmd `0x01` (Get Device ID) for BMC firmware/device IDs | Board serials, asset info, BMC firmware revision — read-only, no write path proposed | S/M | No — same pattern | Nothing. Bonus: this pairs naturally with `errors.py`'s existing `IdentityMismatchError` concept (confirming you're about to act on the board you think you are) |
| 5 | `asmb8_users` (read-only half only: `get_users`/`get_user_access`) | IPMI, netfn `0x06` | Which accounts exist and their privilege level — visibility only, no create/delete/password change | S | No — same pattern | Nothing for the read half. Do not build the write half in this tier — see Tier 3 |
| 6 | `asmb8_network` (read-only half only: `get_net_configuration`) | IPMI, netfn `0x0c` cmd `0x02` | Current IP/netmask/gateway/VLAN as configured — visibility only, no write path | S | No — same pattern | Nothing for the read half. Do not build the write half in this tier — see Tier 3 |

**Why these six, and in this order:** 1 and 2 directly serve the collection's
own documented pain points — a wedged single-occupancy media slot (README's
"Known limitations") and a missing observability channel now that SOL is
confirmed dead. 3 and 4 are cheap, standard, and low-risk, filling in
diagnostic gaps around the same install workflow. 5 and 6's read halves are
included here specifically because they *unblock* their own write
counterparts by proving the wire path works before anyone reaches for a
write — exactly the "read-before-write" sequencing the brief asks for — while
their write halves are deliberately excluded from this tier because of what
they can do wrong.

**Testability note for all six:** none of this needs new mock-server
infrastructure. `tests/integration/mock_servers/ipmi_server.py`'s
`FakeIpmiCommand` already stands in for `pyghmi.ipmi.command.Command` at the
Python object level (not wire level) for `get_power`/`set_power`/
`get_bootdev`/`set_bootdev`/`get_mci`; each new capability above is one more
faked method on that same class, exactly the pattern already established. No
`.asp`-style wire-protocol mock is needed because `pyghmi` itself owns the
wire protocol here — this collection only owns the classification layer on
top of it, same as today.

### Tier 2 — medium value, standard transport, still low risk

| # | Module | Transport | Returns/changes | Effort | Needs hardware? | Discover first? |
|---|---|---|---|---|---|---|
| 7 | `asmb8_identify` | IPMI, netfn `0x00` cmd `0x04` (Chassis Identify, via `pyghmi.set_identify()`'s generic fallback) | Turns the physical chassis ID/UID LED on/off/for a duration | S | No | Nothing |
| 8 | `asmb8_alerts` (read + basic write) | IPMI, netfn `0x0c` cmd `0x02` (PEF LAN Alert Destination) | Report/set SNMP trap (PET) destination(s) and community string | S/M | No | Nothing, but low relevance to this collection's core purpose — a one-shot install doesn't need standing alert infra. Build only if there's a concrete ask for it |

Tier 2 items are here because they're cheap and safe, not because they serve
the headless-install mission directly — rank them below Tier 1 whenever
effort is contended.

### Tier 3 — real capability, deliberately deferred for risk

These have standard, sourced transports and would work. They are held back
specifically because the brief calls this out explicitly: **a mutation here
can brick the board or lock out the very access used to make it**, and that
risk does not go away just because the underlying IPMI command is
well-documented.

| Capability | Why it's deferred | What a much higher confidence bar would require |
|---|---|---|
| `asmb8_users` write half (create/delete/set password, netfn `0x06`) | Can delete or lock out the last usable admin account | Read the current user table first (Tier 1 #5), never touch the account the module itself is authenticating as, dry-run mode, explicit confirmation flag |
| `asmb8_network` write half (set IP/VLAN/gateway, netfn `0x0c` cmd `0x01`) | Can sever the connection making the change, on the interface the change is being made over | Read current config first (Tier 1 #6), require the caller to explicitly confirm the target interface/channel, strongly prefer testing against a board reachable by more than one path first |
| BMC firmware update | `pyghmi` has no fallback path for a generic OEM (§3) — no known-good mechanism exists in this collection's current sourcing at all, before risk is even considered | An actual sourced AMI firmware-update endpoint, which does not currently exist in this project's evidence base, plus a confirmed recovery story (redundant flash bank or equivalent) this hardware may not even have |
| Certificate replacement | Could break the only currently-working TLS trust path (fingerprint pinning) with no way back short of physical access | A sourced `.asp` upload endpoint (none exists today) and a tested rollback story |

### Not achievable / needs new discovery this desk review cannot supply

These are not "later tier" — they have **no sourced transport at all** today,
and building them means the same kind of reverse-engineering work
`asmb8_media` required, not a thin `pyghmi` wrapper:

- **Service enable/disable (write half of `asmb8_redirection`)** — actively
  investigated and not found; needs a browser capture of the BMC's own
  Services admin page.
- **Fan control/policy** — no sourced endpoint; possible OEM raw IPMI command,
  unconfirmed.
- **NTP/time configuration** — `pyghmi`'s own generic OEM fallback proves
  there is no standard command; `.asp`-only, unsourced.
- **Syslog forwarding configuration** — `.asp`-only, unsourced, and
  orthogonal to this collection's purpose.
- **LDAP/AD integration** — no standard IPMI mechanism, no sourced endpoint,
  real lockout risk, low relevance — do not pursue without a concrete reason.
- **Configuration backup/restore** — no standard mechanism, no sourced
  endpoint; restore specifically is high-risk even once discovered.
- **BIOS settings** — no known transport on this hardware generation at all.
- **True DMI/SMBIOS inventory** — this is host-OS data, not a BMC capability;
  FRU inventory (Tier 1 #4) is the achievable substitute.
- **KVM video decoding (`asmb8_console capture=decoded_frame`)** — already a
  documented non-goal, not a proven gap; listed here only for completeness.
- **Anything that would need SOL** — blocked outright; see the top of this
  document.

### Delegate to an existing module instead of writing our own

- **Nothing new to delegate.** `asmb8_power`/`asmb8_boot` already follow this
  principle correctly: they don't literally depend on `community.general`
  (deliberately, to avoid a whole extra collection dependency for two calls —
  see `README.md`'s "Requirements" section), but their option vocabularies
  are copied verbatim from `community.general.ipmi_power`/`ipmi_boot`'s own
  documentation, which is the spirit of "reuse, don't reinvent" without the
  dependency cost.
- **`community.general` has no equivalent for anything in this roadmap.** As
  of this research, that collection's IPMI coverage is exactly
  `ipmi_power`/`ipmi_boot` — nothing for sensors, SEL, FRU, users, network
  config, or BMC self-reset. There is no module to delegate to for any Tier 1
  or Tier 2 item; a thin `asmb8_*` wrapper over `pyghmi`, in the same shape
  as `asmb8_boot`, is the only reuse-consistent option.
- **Worth calling out explicitly:** `community.general.ipmi_power`'s
  `state=reset` resets the *host* via chassis control (netfn `0x00`) — a
  completely different operation from `asmb8_reset`'s proposed BMC self-reset
  (netfn `0x06` cmd `0x02`). Anyone tempted to reach for the existing
  `ipmi_power` module to solve the wedged-media-slot problem would be
  resetting the wrong thing.

---

## 5. Open questions / needs hardware discovery

Listed honestly as "we don't know yet," not asserted either way:

1. **Does this firmware answer DCMI's Get Power Reading at all?**
   `pyghmi.get_system_power_watts()` sends the standard command
   unconditionally (§2, §3), but DCMI compliance is a separate, optional
   certification from base IPMI 2.0, and nothing in this collection's
   evidence confirms it for this board. Cheapest way to answer: one IPMI
   call, once hardware access resumes and it's safe to probe again — no new
   protocol work needed either way, this is purely "does it answer or return
   an error."
2. **Does this board's SEL actually populate with anything?** A BMC that has
   never logged an event would make a freshly-built `asmb8_sel` module look
   broken when it is simply untested against a quiet board. Cheapest way to
   answer: read the SEL once hardware access resumes; if empty, force one
   event (an actual chassis-intrusion or power-loss condition, or the
   documented cold-reset command from Tier 1 #1) and read again.
3. **Redfish, for real.** The repository's generational claim (AST2400
   predates Redfish, which arrived with AST2500/ASMB9) is well-supported by
   independent hardware-generation evidence but not confirmed by a primary
   ASPEED/AMI statement naming this exact board+firmware. Cheapest way to
   answer, once it is safe to make any request to this BMC again: a single
   unauthenticated `GET /redfish/v1/` over HTTPS. If it 404s or the
   connection is refused, the existing claim is confirmed directly rather
   than by inference; this collection should not spend any implementation
   effort on Redfish until that one request has actually been made.
4. **Whether any raw, unsourced OEM IPMI command exists for fan policy or
   NTP.** This desk review found no standard command and no sourced `.asp`
   endpoint for either, but "no command this review could find" is not the
   same claim as "no command exists." Closing this needs either a wire
   capture of the BMC's own web UI performing either action, or a
   `ipmitool raw` sweep against known AMI OEM netfn ranges — both
   hardware-adjacent work outside this review's scope.
5. **Whether the standard IPMI Get Session Info command (netfn `0x06` cmd
   `0x3d`) reports anything useful on this board at all.** Untested; low
   priority given it wouldn't address the `.asp`/iUSB session visibility gap
   that actually matters to this collection, but cheap to confirm alongside
   Tier 1 work if someone is already in the IPMI code.
