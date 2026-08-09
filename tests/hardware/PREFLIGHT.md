<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Pre-flight briefs: escalations 3 and 5

Read the relevant section below **before clicking approve** on
`hardware-media-attach-approval` or `hardware-reset-approval` in the CircleCI
UI. This document is written for the person about to touch real hardware, not
for a code reviewer: it says what can go wrong and what to do about it, not
why the code is structured the way it is (see the corresponding playbook's own
header comment for that, and [`README.md`](README.md) for the escalation
chain as a whole). If you are running one of these two playbooks by hand
instead of through CI, everything below still applies -- "the approver" just
means you.

Escalations 2 (`hardware-login`), 4 (`hardware-boot-once`) and 6
(`hardware-kvm`) do not get their own section here: none of the three can
leave the board or host in a state that needs a physical hand, so their own
playbook header comments are the complete picture. Escalations 3 and 5 are
different, for the reasons below.

---

## Media attach/detach (escalation 3, `media_attach.yml` + `media_detach.yml`)

### What it does

1. `media_attach.yml` provisions a small local test ISO (fetched once from
   `boot.ipxe.org`, never from the BMC) and attaches it to the virtual
   CD-ROM over iUSB.
2. It writes the resulting `session_id` to a file under
   `tests/hardware/output/media/` **before** returning.
3. `media_detach.yml` -- run as a **separate** `ansible-playbook` invocation,
   with `when: always` in `.circleci/config.yml` -- reads that file and
   detaches the same session, regardless of whether the attach step
   succeeded, failed, or the job was cancelled outright.

### What it is looking for

Whether this collection's iUSB client can actually open a session against
real firmware and have the BMC report it attached -- the first genuinely new
hardware evidence this collection would have for its headline capability. See
`docs/hardware-evidence-2026-08-08.md` for what was previously confirmed
through a manual, non-automated session; this is what would move the *same*
finding from "a maintainer's one-off session" to "reproducible through CI".

### Why this needs its own brief

This board's `cd-media` service allows **exactly one active session,
board-wide, with no server-side timeout** to reclaim an abandoned one (see
the top-level README's "Known limitations"). There is no remote "kick the
current holder" call -- if something holds the slot and never closes its TCP
connection, it is held **forever** until a BMC cold reset. `media_detach.yml`
running as a guaranteed cleanup step is the mitigation, but it can only
detach a session it can find, which depends on the state file surviving
whatever went wrong.

### Expected outcome on a healthy board

`media_attach.yml` reports `session_state: attached`. `media_detach.yml`
reports `changed: true` (a live session was actually asked to stop) and the
state file is removed.

### Failure modes

| Failure | What it means | Board left in |
|---|---|---|
| Attach fails with `error_class=bmc_busy` | The slot is already held by something this run's own `runtime_dir` did not know about (a manually-opened JViewer/browser session, or a session opened by a different `runtime_dir`). | Unaffected by this run; the slot was already stuck before it started. |
| Attach fails with `error_class=timeout`, `indeterminate=true` | The session process may still be running and may still confirm `attached` shortly. `asmb8_media` does **not** tear it down on this outcome -- see its own RETURN docs. | Possibly attached, possibly not -- re-probe (`asmb8_media` with the same `session_id`), do not blindly retry. |
| `media_attach.yml`'s process itself dies (job cancelled, runner crash) before it can write the session id file | `media_detach.yml` has nothing to find. | **The slot may be stuck with no local record of it.** |
| `media_detach.yml` runs but the daemon does not exit within `detach_timeout` | Recorded as a warning, not a failure -- the state file is still removed. | Possibly still holding the slot from the BMC's point of view. |

### Recovery

1. Re-run `media_detach.yml` by hand if a session id is still known
   (`tests/hardware/output/media/session-id.txt`, if it survived).
2. If the slot is stuck with no known session id: a BMC cold reset
   (`ipmitool mc reset cold`, or `pyghmi`/`community.general.ipmi_power`'s
   equivalent) resets the management controller only, not host power, and is
   the documented escape hatch for exactly this.
3. This escalation never touches host power or boot configuration -- a stuck
   media slot does not, by itself, put the host in danger.

### Blast radius

**Availability only, scoped to this one board's media slot.** Nothing is
destroyed; the risk is that the only virtual-media slot this board has stays
unusable until a BMC reset, for everyone, including the next legitimate use of
this same capability.

---

## Power-cycle (escalation 5, `reset.yml`)

### What it does

1. Reads the current power state and **refuses to continue** unless it is
   already `on`.
2. Issues `asmb8_power state=reset`.
3. Polls (bounded: 30 retries / 10s delay by default) for the host to report
   `on` again.
4. Independently re-reads the IPMI boot-device override and reports whether it
   reverted to `default` -- the actual confirmation of escalation 4
   (`boot_once.yml`) leaving an optical override armed.

### What it is looking for

Whether `asmb8_power state=reset` genuinely works against real firmware, and
whether a one-time IPMI boot override genuinely reverts to `default` after a
real reset rather than merely being reported as one-time. Per
`docs/hardware-evidence-2026-08-08.md`, this exact claim was previously
confirmed once, manually, on the target board; this is what would make it
reproducible.

### Why this needs its own brief

This is the **most destructive** action in this collection's entire hardware
chain: it is the one escalation that unconditionally power-cycles a host. A
reset that does not come back is a machine that needs a physical hand, not
something any later job in this chain can fix.

### Expected outcome on a healthy host

The host reports `on` again within the poll window, and the boot-device
override reads back as `default`.

### Failure modes

| Failure | What it means | Host left in |
|---|---|---|
| Refused at the pre-check because the host was not already `on` | The host was off, sleeping, or unreachable before this playbook even tried anything. | Untouched -- nothing was sent. |
| `asmb8_power state=reset` itself fails (connection/timeout/auth) | The reset request may or may not have reached the platform -- `error_class=timeout` with `indeterminate=true` means it might have. | **Uncertain.** Re-probe with `asmb8_info`/`asmb8_power state=query` before assuming either way. |
| The host never reports `on` again within the poll window | The reset was accepted but the host did not come back within the budgeted time. | **The host may be down and require a physical hand.** This is the actual stranding risk this escalation carries. |
| The host comes back `on`, but the boot override did **not** revert to `default` | A genuine, new hardware finding worth recording -- see `docs/hardware-evidence-2026-08-08.md`'s framing for what a negative result here would mean for this collection's one-time-boot claim. | On and healthy; only the boot-device claim is in question, not host availability. |

### Recovery

If the host does not come back within the poll window:

1. **Physical power button**, if you are at the machine -- the most direct
   recovery.
2. **KVM console** (`kvm.yml`, or a browser/JViewer session against the
   BMC) to see what the host is actually doing -- a "stuck" machine is often
   sitting at a BIOS/boot prompt waiting for input, especially if a stray
   boot override pointed it somewhere unexpected.
3. **Re-issue `asmb8_power state=on` by hand.** It is convergent, so
   re-running it is always safe.
4. **Physical power cycle / unplug-and-replug AC** as the last resort, if the
   BMC itself is still reachable but the host genuinely will not respond.

### Blast radius

**Availability, and whatever state the host's own OS was in.** Nothing this
collection controls is destroyed by a reset failing to recover -- there is no
irreversible BMC-side mutation here the way, for example, the sibling
`james_crowley.intel_amt` collection's log-clear stage carries. The risk is
entirely that the host itself ends up down and needs a physical hand to bring
back, and that a workload that was running on it before the reset is gone the
moment the reset itself is issued -- **the reset is issued unconditionally
once the pre-check passes, with no attempt to save any running state.**
