<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Role: `james_crowley.asmb8_ikvm.asmb8_baremetal_install`

Hand this role a local ISO; it gets a bare-metal host booting that installer,
with **no PXE/DHCP/TFTP/NFS/CIFS infrastructure required**. That is this
collection's headline capability and this role's entire reason to exist.

```
validate -> probe -> attach ISO over iUSB -> arm one-time optical boot -> reset -> observe -> [wait for hand-off] -> detach
```

This role wraps four modules in this collection (`asmb8_info`, `asmb8_power`,
`asmb8_boot`, `asmb8_media`) with the safety scaffolding a real, physically
disruptive operation needs: a fan-out guard, an explicit destructive-action
confirmation, a preflight reachability check, resumability across interrupted
runs, and a guaranteed media detach even on failure.

**Read this before you run it against real hardware:**

- **A stock installer ISO will NOT install unattended.** See
  ["The stock-ISO limitation"](#the-stock-iso-limitation-read-this-first)
  below.
- **This board's virtual-media slot holds exactly one session, board-wide,
  forever, if nobody closes it.** See
  ["The single-media-session hazard"](#the-single-media-session-hazard) below.
- **Nothing in this role has completed a full, real OS install yet.** See
  ["Verification status"](#verification-status) at the end of this document
  for exactly what has and has not run against real hardware.

## The stock-ISO limitation (read this first)

Verified directly against the target hardware: a stock, unmodified Proxmox VE
installer ISO, booted through this role, reached its branded GRUB menu and
then **sat there waiting for a keypress**, indefinitely. That menu's own
`set timeout=10` line lives inside an `if [ -f auto-installer-mode.toml ]`
conditional in the ISO's own GRUB config -- and a stock ISO does not contain that
marker file. The stock image therefore has **no menu timeout at all**, by design,
so the menu never auto-boots no matter how long you wait. That same conditional
is also what gates the ISO's `Install Proxmox VE (Automated)` menu entry, which
is why preparing the ISO fixes both problems at once.

**Booting a stock installer ISO with this role does not produce an unattended
install.** It produces a machine sitting at a menu, consuming the BMC's only
virtual-media session, until a human intervenes over KVM.

The supported unattended path is an ISO an install tool has already *prepared*
for unattended boot. For Proxmox VE specifically:

```bash
proxmox-auto-install-assistant prepare-iso proxmox-ve.iso \
  --fetch-from iso --answer-file answer.toml \
  --output proxmox-ve-auto.iso
```

This bakes the answer file directly into the ISO and flips its GRUB config to
boot the ISO's own `Install Proxmox VE (Automated)` menu entry, driven by the
`proxmox-start-auto-installer` kernel flag, without waiting for a keypress. The
prepared ISO also carries an `auto-installer-capable` marker at its root.
Point `asmb8_baremetal_install_iso_path` at the *output* of that command, not
at the stock ISO. This collection's own
`james_crowley.asmb8_ikvm.asmb8_autoinstall_iso` role runs that exact step for
you, with its own disk-safety gate -- see the worked example below.

`asmb8_media`, as it exists today, has **no answer-file/floppy slot at all** --
unlike the sibling `james_crowley.intel_amt` collection's `amt_media`, it
attaches exactly one read-only ISO image and nothing else (see its
`DOCUMENTATION`). The answer file must already be inside the ISO you hand this
role; this role has no second slot to carry one separately.

## The single-media-session hazard

This BMC's cd-media/iUSB service allows **exactly one active session, board-
wide**, and has **no server-side timeout** to reclaim an abandoned one. A run
of this role that crashes uncleanly -- controller killed, network partition,
`kill -9` on the wrong process -- can leave a background daemon holding that
one slot open forever. Nothing on the BMC side will ever take it back.

This is why `tasks/main.yml` wraps the whole lifecycle in `block`/`rescue`/
`always`, and why the `always` section detaches media unconditionally whenever
this role's run-state exists (see ["Always detach"](#always-detach) below).
That is a guarantee this role works to provide, not a guarantee the platform
gives you underneath it: a controller that is `kill -9`'d between claiming a
session id and this role's own cleanup running will still strand a daemon
process, because nothing ran to stop it.

**If software reclamation ever fails** -- this role's own `always` block, a
manual `asmb8_media state=detached` call, all of it -- the operator's escape
hatch is a **BMC cold reset** (`ipmitool mc reset cold`, or the
`pyghmi`/`community.general.ipmi_power` equivalent against the BMC's own
management controller). This resets the *BMC*, not the host: it does **not**
power-cycle or otherwise affect the machine the BMC is managing. It is safe to
use to break a wedged media session precisely because of that separation.

`asmb8_media`'s attach also runs an always-on reclamation pass over every
OTHER session this collection's own `runtime_dir` still has a record of
against the same endpoint, before ever attempting a fresh attach (see its
`DOCUMENTATION`). That can only reclaim sessions the SAME `runtime_dir` is
tracking -- it cannot forcibly evict a session held by a different controller,
a manually opened JViewer/browser session, or a daemon whose `runtime_dir` was
deleted out from under it. In those cases the BMC cold reset above is the only
remaining option.

## What this role does *not* decide for you

- Which OS or installer to use, or how its answer file is built. This role
  only gets bytes onto the BMC's virtual CD-ROM and confirms hand-off; it has
  no opinion about what those bytes are. This collection's own
  `james_crowley.asmb8_ikvm.asmb8_autoinstall_iso` role builds that ISO for
  Proxmox VE specifically -- rendering and validating an `answer.toml`, then
  baking it into a copy of a stock ISO -- and carries its own, Proxmox-schema-
  aware disk-safety gate (an explicit disk-list/disk-filter with no wildcard
  allowed, plus its own destructive-confirmation variable; see that role's
  README.md). That is a different, more specific mechanism than this role's
  own `asmb8_baremetal_install_target_disk` forward guard below, which exists
  in case a future feature added directly to *this* role's own lifecycle
  (rather than to ISO preparation) ever needs to name a disk.
- Whether the ISO you hand it is actually auto-install-prepared. This role has
  no way to inspect the ISO's own contents for an `auto-installer-capable`
  marker; if you attach a stock ISO, this role attaches it faithfully and then
  waits at a menu it cannot see, exactly as described above.
- Physical/KVM recovery if a machine ends up in a bad boot state, or a BMC
  cold reset if a media session is stranded beyond what this role's own
  `always` block can reach. Keep that path available *before* running this
  role against new hardware for the first time.

## The single most important variable

```yaml
asmb8_baremetal_install_confirm_destructive: false   # default -- this role does nothing destructive until you flip it
```

This role power-cycles and can reimage the target. It refuses to proceed past
validation unless `asmb8_baremetal_install_confirm_destructive` is explicitly
`true`, and the failure message names the variable. Set it at the point of use
(`-e asmb8_baremetal_install_confirm_destructive=true`), not in a checked-in
defaults file.

## Fan-out guard

`delegate_to: localhost` changes *where* a task runs, not *how many times*. A
play over ten hosts still issues ten resets, `serial: 1` or not. This role
asserts, at runtime, that `ansible_play_hosts_all | length == 1` -- the play's
full host roster, **not** just the current serial batch, is exactly one host.
Point this role at exactly one host per play.

## Expected duration -- do not kill a working install

Measured directly against the target hardware: this board streams the
attached ISO over iUSB at roughly **800 KB/s**, using 16-block (32 KB) reads.
Proxmox's own `pve-installer.squashfs` alone is **614 MB** -- that is **13+
minutes of streaming for one file**, before the installer even starts running
against it. A full unattended install is longer still.

**Idle is normal and has no meaningful upper bound.** Verified directly: an
attached, healthy session went completely silent for **130 consecutive
seconds** while the host sat at a bootloader menu, then resumed serving reads
normally with no intervention at all. A long, quiet wait during
`tasks/wait_for_handoff.yml`, or a long gap between debug output during the
install, is not evidence of a hang.

`asmb8_baremetal_install_handoff_timeout` defaults to `3600` (one hour)
accordingly. Do not shrink it casually.

## Resumability

A separate `ansible-playbook` invocation has no memory of a prior run. This
role gives it one: a small JSON file per target
(`{{ asmb8_baremetal_install_state_dir }}/<asmb8_baremetal_install_host>.json`,
default `~/.ansible/asmb8_ikvm/baremetal-install/`) tracking whether media is
attached, whether one-time boot is armed, whether a reset was issued, and
whether that reset was *confirmed* (the postcondition probe actually observed
the endpoint powered back on).

What this protects against: a run that armed one-time boot and/or issued a
reset, then got interrupted -- Ctrl-C, controller crash, network partition --
before the outcome was confirmed. IPMI power/boot commands are fire-and-forget
once accepted by the BMC; there is nothing to poll for after a reset itself
(see `asmb8_power`'s `DOCUMENTATION`), so a naive re-run that ignored this
history could re-issue a reset against a machine that may already be mid-
install.

- If the state file shows `boot_armed: true` and `reset_confirmed: false`,
  the role refuses to continue on its own. It fails with a message pointing at
  manual verification (KVM/physical console), and requires
  `-e asmb8_baremetal_install_force_resume=true` before it will touch that
  target again.
- Once `asmb8_baremetal_install_force_resume=true` is given, the role clears
  the armed/reset flags and proceeds. Re-arming the boot selection itself is
  genuinely idempotent here -- `asmb8_boot` is a plain IPMI
  `get_bootdev()`/`set_bootdev()` compare, unlike the sibling
  `james_crowley.intel_amt` collection's `amt_boot`, which needs a fresh
  action token to avoid a re-arm being mistaken for a replay. What this role
  still never does automatically is re-issue the *reset* itself.
- If a prior run's `reset_confirmed` was `true` (it finished cleanly), the
  next run starts from a clean slate rather than carrying stale state
  forward.

**Failure mode this does not cover:** if the *controller* itself is different
between runs (a different laptop, a fresh CI container with no persisted
`asmb8_baremetal_install_state_dir`), the state file will not be there and this
role has no way to know a previous, different controller left a target armed.
Point `asmb8_baremetal_install_state_dir` at durable, shared storage if
resumability needs to survive a change of controller.

**Recovering a wedged run:**

1. Check the machine's actual state over KVM/physical console -- has it
   rebooted? Is it mid-install? Stuck at a menu?
2. If media needs cleaning up and you know it is safe to do so, run this
   role's `tasks/detach_media.yml` directly (or call
   `james_crowley.asmb8_ikvm.asmb8_media` with `state: detached` and the
   `session_id`/`runtime_dir` from the state file yourself).
3. If software detach does not work, use a BMC cold reset -- see
   ["The single-media-session hazard"](#the-single-media-session-hazard)
   above.
4. Re-run this role with `-e asmb8_baremetal_install_force_resume=true` once
   you know what actually happened.

## Always detach

Media attach/detach is wrapped in `block`/`rescue`/`always` at the top level
(`tasks/main.yml`): whatever fails -- probe, attach, arm, reset, observe, or
wait-for-hand-off -- the `always` section still runs and detaches media. A
failed install must never leave a background media daemon holding the BMC's
one virtual-media slot open.

For that to be a guarantee rather than an intention, the role claims and
persists its media `session_id` **before** `asmb8_media` can spawn anything,
not after the attach returns -- see `tasks/attach_media.yml`'s own comment for
the exact failure mode this avoids. If a run fails, the session id and its
runtime directory appear in the failure output (`tasks/record_failure.yml`)
along with the manual-recovery hint above, so that in the one case the
`always` block cannot cover -- a controller killed outright -- the operator
can still find what to clean up.

## Unresolved: Linux's own USB re-enumeration

Once Linux boots, it re-enumerates USB storage through its own kernel driver,
independently of whatever got the firmware/bootloader to boot from this same
device. Whether that re-enumeration needs a **separate** iUSB session -- one
the BMC's single-occupancy media slot might then deny while this role's own
daemon is still holding it -- **has not been tested on real hardware.** This
is genuinely unknown, not merely undocumented.

`asmb8_baremetal_install_media_release_after_handoff` (default `false`) is the
extension point for this: set it `true` to make this role detach media right
after the postcondition probe confirms IPMI observed power back on, and before
the hand-off wait -- i.e. right around where Linux's own boot/USB re-init would
happen. **Do not flip this on for a real install without first proving the
installer no longer needs this session at that point** -- an installer that
streams more of the ISO afterward (which describes an ordinary Proxmox
install, still mid-stream at that moment) will very likely break if the media
disappears out from under it. See `defaults/main.yml`'s comment on this
variable for the same caveat in full.

## Worked example: Proxmox VE

Preparing the auto-installing ISO and booting from it are two separate roles
in this collection, run back to back. `asmb8_autoinstall_iso` never talks to a
BMC (it only ever builds a local file); `asmb8_baremetal_install` never
touches the ISO's contents (it only ever attaches whatever file it is given).
Each has its own destructive-confirmation gate -- setting one does not set the
other.

```yaml
- name: Prepare an auto-installing Proxmox VE ISO, then boot it onto a bare-metal machine
  hosts: "{{ target }}"       # exactly one host -- see the fan-out guard above
  serial: 1
  gather_facts: false
  connection: local

  vars:
    asmb8_autoinstall_iso_confirm_destructive: false   # override at the point of use -- see that role's README.md
    asmb8_autoinstall_iso_source: /srv/images/proxmox-ve.iso
    asmb8_autoinstall_iso_output: /srv/images/proxmox-ve-auto.iso
    # Stable-identifier disk targeting is that role's own gate, not this
    # role's asmb8_baremetal_install_target_disk -- see
    # asmb8_autoinstall_iso's README.md "Disk safety" before setting this.
    #
    # The two roles deliberately use DIFFERENT identifier conventions. This is
    # not an inconsistency to tidy up:
    #
    #   * asmb8_baremetal_install_target_disk is a local safety record. It is
    #     never sent to the installer, so it demands a stable form
    #     (/dev/disk/by-id/..., /dev/disk/by-path/..., or serial:...) purely so a
    #     human reading the playbook can tell which physical device is about to
    #     be overwritten.
    #   * asmb8_autoinstall_iso_answer_disk_filter is written into Proxmox's
    #     answer.toml. Proxmox's own disk-list field accepts only BARE KERNEL
    #     NAMES (sda, nvme0n1) and will not take a /dev/disk/by-id path -- yet
    #     kernel names are not stable across boots. The filter form below is
    #     therefore the stable mechanism: it matches udev properties such as
    #     ID_SERIAL_SHORT instead of an enumeration-order name.
    asmb8_autoinstall_iso_answer_disk_filter: { ID_SERIAL_SHORT: "S123456789NVME" }

    asmb8_baremetal_install_confirm_destructive: false   # override at the point of use, deliberately
    asmb8_baremetal_install_host: "{{ bmc_management_address }}"
    asmb8_baremetal_install_username: "{{ bmc_admin_username }}"
    asmb8_baremetal_install_password: "{{ vaulted_bmc_password }}"
    asmb8_baremetal_install_tls_fingerprint: "{{ vaulted_bmc_tls_fingerprint }}"

    # A stock ISO will attach and boot, but will not install unattended --
    # see "The stock-ISO limitation" above. Point this at
    # asmb8_autoinstall_iso_output above, not at the stock source ISO.
    asmb8_baremetal_install_iso_path: "{{ asmb8_autoinstall_iso_output }}"

    asmb8_baremetal_install_wait_for_handoff: true
    asmb8_baremetal_install_handoff_host: "{{ provisioned_host_address }}"
    asmb8_baremetal_install_handoff_port: 22
    asmb8_baremetal_install_handoff_timeout: 3600     # a real install can run a long time unattended
    asmb8_baremetal_install_handoff_delay: 120

  roles:
    - james_crowley.asmb8_ikvm.asmb8_autoinstall_iso
    - james_crowley.asmb8_ikvm.asmb8_baremetal_install
```

See `asmb8_autoinstall_iso`'s own README.md for everything that role's
variables above actually mean, and for its disk-safety gate in full -- it is
not repeated here.

## Variables

### Connection (mirrors the `connection` doc fragment)

There is no inventory-variable fallback convention here (unlike the sibling
`james_crowley.intel_amt` collection's `amt_baremetal_install`, which falls
back to conventional `amt_*` variables established elsewhere in that
collection) -- nothing in *this* collection has established an equivalent
convention yet, and this role does not invent one unilaterally. Every
connection variable below is this role's own, with no fallback.

| Variable | Default | Notes |
|---|---|---|
| `asmb8_baremetal_install_host` | `null` (**required**) | BMC web-management address. Does not fall back to `inventory_hostname` -- that names the OS this role is about to install, not the BMC managing it |
| `asmb8_baremetal_install_port` | `443` | BMC web-management HTTPS/HTTP port (the `.asp`/JNLP plane `asmb8_media` uses) |
| `asmb8_baremetal_install_username` | `admin` | |
| `asmb8_baremetal_install_password` | `null` (**required**) | Always vaulted |
| `asmb8_baremetal_install_use_tls` | `true` | |
| `asmb8_baremetal_install_allow_insecure_transport` | `false` | Required alongside `asmb8_baremetal_install_use_tls: false` |
| `asmb8_baremetal_install_validate_certs` | `true` | In practice cannot succeed against this board's expired, self-signed factory certificate without `tls_fingerprint` pinning instead -- see the `connection` doc fragment |
| `asmb8_baremetal_install_ca_path` | `null` | Mutually exclusive with `asmb8_baremetal_install_tls_fingerprint` |
| `asmb8_baremetal_install_tls_fingerprint` | `null` | Required when `asmb8_baremetal_install_use_tls: true`. This board's recommended trust mode -- see the `connection` doc fragment |
| `asmb8_baremetal_install_timeout` / `asmb8_baremetal_install_connect_timeout` | `30` / `10` | |
| `asmb8_baremetal_install_ipmi_port` | `623` | UDP RMCP+ listener, used by `asmb8_info`/`asmb8_power`/`asmb8_boot` only -- **not** part of the `module_defaults` group this role sets, because `asmb8_media` has no `ipmi_port` option at all |

### Lifecycle

| Variable | Default | Notes |
|---|---|---|
| `asmb8_baremetal_install_confirm_destructive` | `false` | **Required `true` to do anything destructive** |
| `asmb8_baremetal_install_iso_path` | `null` (**required**) | Local ISO attached read-only over iUSB. See ["The stock-ISO limitation"](#the-stock-iso-limitation-read-this-first) |
| `asmb8_baremetal_install_media_cd_port` | `5120` | BMC's iUSB virtual CD-ROM listener. Refuses connections until a session is allocated -- not an error |
| `asmb8_baremetal_install_media_instance` | `0` | iUSB device-slot instance; `0` is the only configuration validated |
| `asmb8_baremetal_install_media_runtime_dir` | `~/.ansible/asmb8_ikvm/media-sessions` | Must match across attach/detach |
| `asmb8_baremetal_install_media_attach_timeout` | `30` | Bounds confirming the attach started, **not** the install duration |
| `asmb8_baremetal_install_media_detach_timeout` | `30` | |
| `asmb8_baremetal_install_boot_device` | `optical` | The only value this role has ever driven -- paired with the CD-ROM slot `asmb8_media` attaches to |
| `asmb8_baremetal_install_boot_uefi` | `false` | |
| `asmb8_baremetal_install_observe_retries` / `asmb8_baremetal_install_observe_delay` | `10` / `15` | Bounded postcondition IPMI power probe |
| `asmb8_baremetal_install_wait_for_handoff` | `true` | Set `false` to skip the final wait |
| `asmb8_baremetal_install_handoff_host` | `null` (**required** when `asmb8_baremetal_install_wait_for_handoff` is `true`) | The address the freshly installed **OS** answers on -- not the BMC address |
| `asmb8_baremetal_install_handoff_port` | `22` | |
| `asmb8_baremetal_install_handoff_timeout` / `asmb8_baremetal_install_handoff_delay` | `3600` / `120` | See ["Expected duration"](#expected-duration----do-not-kill-a-working-install) |
| `asmb8_baremetal_install_media_release_after_handoff` | `false` | **Untested extension point** -- see ["Unresolved: Linux's own USB re-enumeration"](#unresolved-linuxs-own-usb-re-enumeration) |
| `asmb8_baremetal_install_target_disk` | `null` | **Unused today.** Forward guard for a disk-pinning requirement on a not-yet-existing answer-file feature -- see `defaults/main.yml`'s comment and `tasks/validate.yml` |
| `asmb8_baremetal_install_state_dir` | `~/.ansible/asmb8_ikvm/baremetal-install` | Resumability state; needs durable, per-controller-shared storage |
| `asmb8_baremetal_install_force_resume` | `false` | Human-only override past an uncertain prior boot/reset |

## Idempotence

- Re-running this role against a target with `reset_confirmed: true` in its
  state file starts a brand-new install attempt from a clean slate -- exactly
  as if no state file existed. This role does not protect you from
  intentionally reinstalling an already-installed machine; that is what
  `asmb8_baremetal_install_confirm_destructive` is for.
- Re-running against a target with an unconfirmed prior boot/reset fails
  closed, by design. See ["Resumability"](#resumability) above.
- `asmb8_media`'s attach is idempotent against a live `session_id`;
  `asmb8_boot` is idempotent by IPMI compare (unlike the sibling collection's
  action-token-gated `amt_boot`) -- which is why this role never worries about
  re-arming, only about re-issuing the reset that consumes the arm.

## Verification status

**Nothing in this role has completed a full, real OS install yet.** Stated
specifically, this is what has and has not been exercised:

- **The individual modules this role wraps** (`asmb8_info`, `asmb8_power`,
  `asmb8_boot`, `asmb8_media`) are covered by this collection's unit test
  suite against mocked `pyghmi`/HTTP clients -- never a real socket, and never
  the target BMC at a real BMC or any other real board. See each
  module's own `DOCUMENTATION` for what has and has not been verified directly
  against real hardware at the protocol level.
- **This role's orchestration of those modules together** -- the actual
  validate/probe/attach/arm/reset/observe/hand-off/detach sequence,
  end-to-end -- has not run against real hardware, and has not run against a
  mock server either. `tests/integration/targets/asmb8_baremetal_install_role/`
  exists and is structured to drive this role against local mock `.asp`/iUSB
  servers, but currently skips: there is no mock IPMI/RMCP+ listener anywhere
  in this collection yet, and this role's `probe`/`arm_boot`/`reset`/`observe`
  phases all depend on real IPMI (`asmb8_info`/`asmb8_power`/`asmb8_boot`).
  Without that, the role cannot get past its very first task against anything
  but a real BMC. See that target's `tasks/main.yml` for the exact skip
  message and what specifically is missing.
- **No stage of this role has driven a real install to completion.** The
  hand-off wait, the stock-ISO-vs-prepared-ISO distinction, and the single-
  media-session hazard's real-world failure mode are all documented from
  direct hardware measurement (see the sections above), but none of them has
  been exercised by actually running this role start to finish against the lab
  board.
- **`asmb8_baremetal_install_media_release_after_handoff`** is, by its own
  documentation above, an untested extension point. Its default (`false`)
  reflects the only behaviour that has any basis at all -- media stays
  attached throughout -- not a confirmed-safe alternative.

This section will be updated the day any of the above actually runs against
real hardware. Until then, treat every claim of "this role does X" above as a
claim about what the code is written to do, not a claim about what has been
observed to happen.
