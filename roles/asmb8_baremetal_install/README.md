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
hatch is a **BMC cold reset** (`ipmitool mc reset cold`, the
`pyghmi`/`community.general.ipmi_power` equivalent, or this collection's own
`james_crowley.asmb8_ikvm.asmb8_reset` module with `mode: cold`, which is
exactly that manual step promoted to a first-class, testable module). This
resets the *BMC*, not the host: verified live against the target hardware, the
host stayed powered on and completely unaffected throughout a cold reset. It
is safe to use to break a wedged media session precisely because of that
separation -- but it drops every other active BMC session too (IPMI, `.asp`
web logins, any other in-flight media session), and recovery of the BMC
afterwards is staged (ICMP answers before the `.asp`/HTTPS stack does), so do
not assume the BMC is fully usable again the instant it starts responding to
ping. See `asmb8_reset`'s own `DOCUMENTATION` for the full detail.

**A wedged session does not necessarily look wedged from the network layer.**
Observed directly on the target hardware: a stuck media session's TCP
connection to the iUSB port was fully `ESTABLISHED`, with bytes sitting unread
in the socket's own receive queue, while zero SCSI commands were actually
being serviced. An `ESTABLISHED` connection on port 5120 is therefore **not**
evidence media is being served -- check `asmb8_media`'s own `session_state`/
`bytes_read`/`sectors_served` (or the idle-streak fields described in
["Distinguishing idle from a broken connection"](#distinguishing-idle-from-a-broken-connection)
below) before concluding a session is healthy just because something answers
on that port.

`asmb8_media`'s attach also runs an always-on reclamation pass over every
OTHER session this collection's own `runtime_dir` still has a record of
against the same endpoint, before ever attempting a fresh attach (see its
`DOCUMENTATION`). That can only reclaim sessions the SAME `runtime_dir` is
tracking -- it cannot forcibly evict a session held by a different controller,
a manually opened JViewer/browser session, or a daemon whose `runtime_dir` was
deleted out from under it. In those cases the BMC cold reset above is the only
remaining option.

### Interrupting a play, and what this collection does about it

Killing the controller process outright (`kill -9`, a hard host crash) is not
the only way to leave a media daemon running with nobody left to detach it.
**Interrupting the play itself** -- `Ctrl-C` on `ansible-playbook`, a CI job
cancelling a running step, a supervisor sending a termination signal to the
whole process group -- reaches `asmb8_media`'s own background daemon too, and
on firmware with no server-side reclaim timeout for `cd-media` (see above),
a daemon that does not tear its session down cleanly on that signal strands
the slot exactly as permanently as a `kill -9` would. This collection's
background daemons install a handler for **`SIGTERM` specifically** and treat
it as equivalent to a normal `state=detached` request -- closing the iUSB
session (which sends the TCP `FIN` the BMC needs to see to free the slot)
through the same code path a clean detach already uses, not a second,
signal-specific one. **`SIGINT` is deliberately not relied upon anywhere in
this path**: a process started in the background by a shell inherits
`SIG_IGN` for `SIGINT` from that shell's own job control, so `kill -INT` (or
an interactive `Ctrl-C` reaching only the foreground process group) can be
silently swallowed by a backgrounded daemon and never invoke a handler at
all -- this was observed directly during this collection's own development,
where a stray `pkill` against a test harness left a session's slot stranded
for exactly this reason, and the eventual fix was recognising that `SIGTERM`,
not `SIGINT`, is the signal a daemon can actually count on receiving when
something upstream wants it to stop.

**This closes the gap for a signal the daemon actually receives.** It does
not, and cannot, help if the interruption is severe enough that the daemon
never gets scheduled again at all (a `SIGKILL`, a hard power loss, an OOM
kill) -- ordinary process termination in those cases still closes the
daemon's own file descriptors as part of exit, including the TCP connection
to the BMC, so this is usually still recoverable without a cold reset, but it
is not a guarantee this collection can make the way it can for `SIGTERM`.

**The symptom of a stranded slot is easy to misdiagnose, and worth stating
precisely** (this is the same fact the "wedged session" note above gives,
restated with the one discriminator that actually settles it): a TCP
connection to port 5120 showing `ESTABLISHED`, with bytes sitting unread in
the socket's own receive queue, is consistent with BOTH a healthy session
that is merely idle (see
["Distinguishing idle from a broken connection"](#distinguishing-idle-from-a-broken-connection))
AND a stranded one where **zero** SCSI commands are ever serviced. An
established socket is not, by itself, evidence that media is being served.
The thing that actually distinguishes the two is whether the daemon's own log
recorded a `vmedia: redirection accepted (instance N, port 5120)` line for
this session at attach time -- present in every session that actually
completed its handshake, absent in one that never did. Check that line (and
`asmb8_media`'s own `session_state`/`bytes_read`/`sectors_served`) before
concluding a socket answering on that port means the media is actually being
served.

**The recovery path when this collection's own software reclamation cannot
see the session** (because its `runtime_dir` was lost, or the holder is a
different controller entirely) is, as above, `asmb8_reset` with `mode: cold`
-- it does not affect host power, only the BMC's own management controller.

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
attached ISO over iUSB at roughly **790-800 KB/s**, using 16-block (32 KB)
reads. Proxmox's own `pve-installer.squashfs` alone is **614 MB** -- that is
**13+ minutes of streaming for one file**, before the installer even starts
running against it. A full unattended install is longer still.

**Idle is normal and has no meaningful upper bound.** Verified directly: an
attached, healthy session went completely silent for **130 consecutive
seconds** while the host sat at a bootloader menu, then resumed serving reads
normally with no intervention at all. A long, quiet wait during
`tasks/wait_for_handoff.yml`, or a long gap between debug output during the
install, is not evidence of a hang.

**A real install was killed by too-short a default.** `handoff_timeout` used
to default to `3600` (one hour), and a real, unattended Proxmox install
against the target hardware was killed by that timeout at **70% complete** --
the install needed longer, not less. That default was too tight and has been
raised; the arithmetic behind the new default follows, so you can size your
own value for your own ISO rather than guessing:

1. **Raw transfer time.** A 1,628 MiB installer ISO at the measured ~790 KB/s
   takes `1,628 * 1024 / 790 ≈ 2,111 s ≈ 35.2 minutes` just to stream once,
   before the installer does anything else with it. This is a floor, not an
   estimate of total install time -- an installer typically re-reads parts of
   the ISO non-sequentially and spends real time unpacking/configuring
   packages on top of the streaming itself.
2. **What the real failure implies about total duration.** The run that was
   killed had completed 70% of its work at the old 60-minute (3,600 s) cutoff.
   Extrapolating linearly, a full run needed at least
   `3,600 / 0.70 ≈ 5,143 s ≈ 85.7 minutes` -- and that is a *floor* derived
   from an incomplete run, not a measured total; the real figure could be
   higher.
3. **New default.** `asmb8_baremetal_install_handoff_timeout` is now `7200`
   (two hours) -- comfortably above both the 85.7-minute floor from step 2
   (about 40% headroom) and more than 3x the raw 35-minute transfer time from
   step 1, to absorb idle stretches (confirmed normal, up to 130+ seconds at a
   time) and postinstall configuration that the transfer-time figure alone
   does not capture.
4. **Sizing your own value.** Do not just trust the default for a
   dissimilar ISO or workload -- compute your own floor the same way:
   `handoff_timeout ≈ (your_iso_size_MiB * 1024 / 790) * safety_factor`,
   where a `safety_factor` of at least 2-3x the raw transfer time is a
   reasonable starting point given how far short the raw transfer estimate
   alone fell for Proxmox above. For a larger ISO, or an installer known to
   do a lot of post-unpack configuration, prefer the higher end of that
   range -- or size directly off a real run's own progress the way step 2
   above did, if you have one.

Do not shrink `asmb8_baremetal_install_handoff_timeout` casually, and do not
mistake a long, quiet `wait_for_handoff` for a hang -- see the idle-versus-
broken guidance immediately below for how to tell the difference from the
media session's own state file while a run is still in progress.

## Distinguishing idle from a broken connection

The same real incident above also produced a genuinely ambiguous trace:
stretches of zero reads that were read, at the time, as "the installer is
unpacking packages" -- when at least one such stretch may actually have been
a network outage between the controller and the BMC. On the guest side, the
symptom was SCSI timeouts and I/O errors; tellingly, the guest logged **zero
`REQUEST_SENSE` commands**, meaning it never received an error status at
all -- it simply never got answered. That is a timeout signature, not an
error-reply signature, on both sides of the connection.

`asmb8_media`'s background session's state file now records enough to make
this separable after the fact, without needing to have been watching live at
the time:

- `operation.observed.current_idle_streak` -- while the session is quiet,
  this is a dict (`started_at`, `polls`, `seconds`) tracking the *current*,
  still-open stretch of silence. It is cleared back to `null` the moment a
  real SCSI request arrives.
- `operation.observed.last_idle_streak` -- the most recently *closed* stretch
  of silence (`started_at`, `ended_at`, `polls`, `seconds`), including the one
  that was open when the session ended, if it ended while idle. Unlike
  `current_idle_streak`, this is not cleared by later traffic, so it stays
  visible for a post-mortem even after the session resumed and kept running.
- `operation.observed.idle_polls` -- a lifetime count of idle heartbeats,
  for a coarse "how quiet has this session been overall" figure.

None of this is real-time network-failure detection -- a network partition
that happens to occur exactly between two SCSI requests still looks, from
this session's own vantage point, identical to a healthy, willingly-idle
host (see `plugins/module_utils/iusb.py`'s `IdleTimeout`/`recv_exact` split
for exactly why: an idle timeout at a frame boundary carries no information
about *why* nothing arrived). What these fields *do* give you is enough
recorded detail -- exact start/end timestamps and durations for every
quiet stretch -- to cross-reference against independent evidence after the
fact: the guest's own kernel/SCSI timeout log timestamps, BMC-side logs, or
network monitoring for the same window. A connection that has genuinely
broken (a stalled read mid-frame, not at a boundary; a socket error) is
reported quite differently and always was, before this change --
`session_state=error` with `error_class=connection` and a message naming the
stall -- so the combination to look for in a post-mortem is: no
`session_state=error` recorded, but a `last_idle_streak`/`current_idle_streak`
duration that lines up suspiciously well with an independently-observed
outage window.

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
    asmb8_baremetal_install_handoff_timeout: 7200     # a real install can run a long time unattended -- see "Expected duration" above
    asmb8_baremetal_install_handoff_delay: 120

  roles:
    - james_crowley.asmb8_ikvm.asmb8_autoinstall_iso
    - james_crowley.asmb8_ikvm.asmb8_baremetal_install
```

See `asmb8_autoinstall_iso`'s own README.md for everything that role's
variables above actually mean, and for its disk-safety gate in full -- it is
not repeated here.

## `ipxe_http` delivery mode

`asmb8_baremetal_install_delivery` has two values:

- **`full_iso` (the default).** Stream the whole ISO named by
  `asmb8_baremetal_install_iso_path` over iUSB, exactly as this role has
  always worked. Nothing about `ipxe_http` existing changes this mode's own
  behaviour.
- **`ipxe_http`.** Attach only a tiny iPXE bootstrap image over iUSB
  (`james_crowley.asmb8_ikvm.asmb8_bootstrap_image`, size-budget-capped),
  whose one job is bringing up the target's real NIC and fetching everything
  Proxmox-sized over plain HTTP from an ephemeral origin this role starts
  and stops itself (`james_crowley.asmb8_ikvm.asmb8_http_origin`). This is
  the fix for exactly the problem ["Expected duration"](#expected-duration----do-not-kill-a-working-install)
  above describes: streaming a 1,628 MiB installer ISO at this board's
  measured ~790-800 KB/s iUSB throughput is 35+ minutes, and a real
  unattended install was once killed mid-run by a too-short timeout at that
  same bottleneck. `ipxe_http` moves the bulk transfer off iUSB entirely, at
  LAN speed, over the target's own NIC.

  See [`docs/netboot-design.md`](/docs/netboot-design.md) for the research
  behind this path and [`asmb8_bootstrap_image`'s own
  documentation](/docs/asmb8_bootstrap_image.md) for exactly what it builds
  and what remains unverified.

**This role does not run `proxmox-auto-install-assistant --pxe` for you, and
does not edit a generated `boot.ipxe` for you.** Both are manual
prerequisites `tasks/validate.yml` can verify were *named* (the relevant
variables are set) but not that they were *done correctly*. The sequence,
per `docs/netboot-design.md` sections 1 and 3:

```bash
proxmox-auto-install-assistant prepare-iso proxmox-ve_9.2-1.iso \
  --fetch-from iso --answer-file answer.toml \
  --pxe --pxe-loader ipxe \
  --output /srv/netboot/proxmox-auto/
```

This produces `vmlinuz`, `initrd.img`, a stripped copy of the source ISO, and
a generated `boot.ipxe` that opens with an unconditional `dhcp` command --
**replace only that `dhcp` line** with the static `set net0/ip ...`/`ifopen
net0` block `docs/netboot-design.md` section 5 documents (the bootstrap
image this role builds already brought the NIC up once; a second DHCP
attempt inside `boot.ipxe` would defeat the point). Point
`asmb8_baremetal_install_ipxe_origin_path` at that directory once edited.

**Proxmox's own netboot mechanism is what makes the read-timeout failure
mode structurally impossible, not merely less likely.** Per
`docs/netboot-design.md` section 2-3 (citing `proxmox-auto-install-assistant`'s
own source, not this collection's own testing): `--pxe`'s generated
`boot.ipxe` never `sanboot`s the installer ISO. It loads the kernel and
initrd as ordinary HTTP fetches, then injects the **entire** installer ISO
into the booted kernel's initramfs as a second, raw-named `initrd` segment --
i.e. the whole ISO ends up resident in RAM before the installer's own init
script ever runs. Once that transfer is over, the installer performs **no CD
reads at all** for the rest of the install -- there is no iUSB channel left
in the loop at that point to time out. This is `docs/netboot-design.md`'s own
reading of Proxmox's source, not something this collection independently
verified by running the tool; treat it with the same confidence that
document states for it, no higher.

### The origin's lifetime cap -- sized from the hand-off timeout, not a guess

During this design's own investigation, an ephemeral HTTP origin was capped
at a flat 30 minutes and expired mid-run, so a subsequent boot attempt hit a
dead server -- a real, previously-observed failure, not a hypothetical one.
`asmb8_baremetal_install_ipxe_origin_lifetime_seconds` exists specifically
so this cannot happen by default: it is computed from
`asmb8_baremetal_install_handoff_timeout` (the same figure
["Expected duration"](#expected-duration----do-not-kill-a-working-install)
sizes for the whole install) plus the postcondition-probe window plus a
fixed safety margin, rather than a second, independent number a caller could
raise one of and forget the other. **The origin has to stay alive for every
byte fetched throughout the whole install, not merely through hand-off
confirmation** -- if you override this directly, make sure it still clears
`asmb8_baremetal_install_handoff_timeout` with real headroom.

### POST-code sampling across the hand-off wait

`asmb8_postcode` exists because IPMI Serial-over-LAN does not work on this
board and `asmb8_console`'s video channel cannot be decoded into pixels --
see that module's own `DOCUMENTATION`, which calls it "the highest-value
module in this batch" for exactly this reason: it is the only out-of-band
signal of boot progress this collection has at all. `tasks/wait_for_handoff.yml`
now samples it in bounded chunks throughout the wait, so a failed install
reports the last BIOS POST code actually observed instead of only "timed
out" -- the single biggest diagnostic improvement available here, for close
to no cost.

`asmb8_baremetal_install_sample_post_code_during_handoff` defaults to
enabled for `ipxe_http` and disabled for `full_iso`, so `full_iso`'s own
behaviour is unchanged by this option's existence; override either
explicitly if you want the opposite. When it fires, the failure message
names the last POST code observed and the full sampled history -- see
`asmb8_postcode`'s own `DOCUMENTATION` for why no meaning is attached to any
individual code.

### Worked example: `ipxe_http`

```yaml
- name: Boot Proxmox VE with the ipxe_http delivery mode
  hosts: "{{ target }}"
  serial: 1
  gather_facts: false
  connection: local
  vars:
    asmb8_baremetal_install_confirm_destructive: false   # override at the point of use
    asmb8_baremetal_install_host: "{{ bmc_management_address }}"
    asmb8_baremetal_install_username: "{{ bmc_admin_username }}"
    asmb8_baremetal_install_password: "{{ vaulted_bmc_password }}"
    asmb8_baremetal_install_tls_fingerprint: "{{ vaulted_bmc_tls_fingerprint }}"

    asmb8_baremetal_install_delivery: ipxe_http
    # Staged by hand per "ipxe_http delivery mode" above:
    # `proxmox-auto-install-assistant --pxe --pxe-loader ipxe`, with the
    # generated boot.ipxe's leading `dhcp` line already replaced.
    asmb8_baremetal_install_ipxe_origin_path: /srv/netboot/proxmox-auto
    asmb8_baremetal_install_ipxe_origin_bind_address: 192.0.2.5   # the controller's address on the target's boot network
    asmb8_baremetal_install_ipxe_lkrn_path: /srv/netboot/ipxe.lkrn
    asmb8_baremetal_install_ipxe_address: 192.0.2.50
    asmb8_baremetal_install_ipxe_netmask: 255.255.255.0
    asmb8_baremetal_install_ipxe_gateway: 192.0.2.1

    asmb8_baremetal_install_wait_for_handoff: true
    asmb8_baremetal_install_handoff_host: "{{ provisioned_host_address }}"
    asmb8_baremetal_install_handoff_timeout: 7200

  roles:
    - james_crowley.asmb8_ikvm.asmb8_baremetal_install
```

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
| `asmb8_baremetal_install_delivery` | `full_iso` | `full_iso` or `ipxe_http` -- see ["`ipxe_http` delivery mode"](#ipxe_http-delivery-mode) |
| `asmb8_baremetal_install_iso_path` | `null` (**required** for `full_iso`) | Local ISO attached read-only over iUSB. See ["The stock-ISO limitation"](#the-stock-iso-limitation-read-this-first) |
| `asmb8_baremetal_install_ipxe_origin_path` | `null` (**required** for `ipxe_http`) | Directory served over HTTP -- typically `proxmox-auto-install-assistant --pxe`'s own output, edited per the section above |
| `asmb8_baremetal_install_ipxe_script_name` | `boot.ipxe` | Filename, relative to `asmb8_baremetal_install_ipxe_origin_path`, the bootstrap image chains to |
| `asmb8_baremetal_install_ipxe_origin_bind_address` | `null` (**required** for `ipxe_http`) | Address the target's real NIC can reach -- almost never the controller's loopback interface |
| `asmb8_baremetal_install_ipxe_origin_port` | `0` | `0` asks the OS for a free ephemeral port |
| `asmb8_baremetal_install_ipxe_origin_runtime_dir` | `~/.ansible/asmb8_ikvm/http-origins` | Must match across this role's own start/stop of the origin |
| `asmb8_baremetal_install_ipxe_origin_start_timeout` / `_stop_timeout` | `10` / `15` | |
| `asmb8_baremetal_install_ipxe_origin_lifetime_seconds` | computed from `asmb8_baremetal_install_handoff_timeout` | **Do not shrink below the hand-off timeout** -- see ["The origin's lifetime cap"](#the-origins-lifetime-cap----sized-from-the-hand-off-timeout-not-a-guess) |
| `asmb8_baremetal_install_ipxe_lkrn_path` | `null` (**required** for `ipxe_http`) | Prebuilt `ipxe.lkrn`, never fetched by this role or `asmb8_bootstrap_image` itself |
| `asmb8_baremetal_install_ipxe_bootstrap_output` | `{{ asmb8_baremetal_install_state_dir }}/bootstrap.iso` | Where the built bootstrap image is written |
| `asmb8_baremetal_install_ipxe_size_budget_bytes` | `16777216` (16 MiB) | Enforced by `asmb8_bootstrap_image` |
| `asmb8_baremetal_install_ipxe_network_mode` | `static` | `static` or `dhcp` -- see `asmb8_bootstrap_image`'s own `DOCUMENTATION` for why `static` is the default |
| `asmb8_baremetal_install_ipxe_address` / `_netmask` / `_gateway` / `_dns` | `null` | Required (except `_dns`) when `asmb8_baremetal_install_ipxe_network_mode=static` |
| `asmb8_baremetal_install_sample_post_code_during_handoff` | `true` for `ipxe_http`, `false` for `full_iso` | See ["POST-code sampling"](#post-code-sampling-across-the-hand-off-wait) |
| `asmb8_baremetal_install_post_code_poll_interval_seconds` | `60` | Seconds between `asmb8_postcode` reads while sampling |
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
| `asmb8_baremetal_install_handoff_timeout` / `asmb8_baremetal_install_handoff_delay` | `7200` / `120` | See ["Expected duration"](#expected-duration----do-not-kill-a-working-install) for the sizing arithmetic |
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
- **`asmb8_baremetal_install_delivery=ipxe_http` has never run against real
  hardware, a mock server, or even a real `grub-mkrescue`.** Its unit-level
  pieces are tested in isolation (`asmb8_bootstrap_image`'s own size-budget/
  missing-tool/check-mode paths, `asmb8_http_origin`'s own real-fork tests)
  and this role's own Jinja/task-flow logic for it was smoke-tested by hand
  against loopback-only mocks and fakes while building it -- but nothing has
  confirmed the built bootstrap image actually boots on this board, or that
  `proxmox-auto-install-assistant --pxe`'s output behaves as
  `docs/netboot-design.md` describes. See that document's own section 8 and
  section 10 for exactly what remains open, and `asmb8_bootstrap_image`'s
  own `DOCUMENTATION` for the GRUB2-syntax uncertainty specifically.

This section will be updated the day any of the above actually runs against
real hardware. Until then, treat every claim of "this role does X" above as a
claim about what the code is written to do, not a claim about what has been
observed to happen.
