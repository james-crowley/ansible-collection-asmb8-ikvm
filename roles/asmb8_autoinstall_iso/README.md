<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_autoinstall_iso`

> **This role prepares an ISO that, once booted, erases a disk with no
> prompt.** `asmb8_autoinstall_iso_answer_disk_list` /
> `asmb8_autoinstall_iso_answer_disk_filter` name the disk(s) that happens to.
> There is **no default target** and this role will not guess one. Read
> "Disk safety" below in full before setting either variable, especially if
> the target machine has more than one drive.

Prepares a stock Proxmox VE installer ISO for **genuinely unattended**
installation: renders and validates an `answer.toml` from role variables,
then bakes it into a copy of the ISO so the ISO itself boots straight into
the installer with no human at the console. This is the step that turns
"boots an installer that waits for a human" into an actually unattended
bare-metal install when driven together with `asmb8_baremetal_install` (or
any other role/playbook that attaches an ISO to a BMC's virtual media and
arms a one-time boot).

This role runs **entirely on the Ansible controller**. It never contacts a
BMC, never makes a network request to install-time infrastructure, and never
opens, formats, or writes to a block device — the only things it reads or
writes are ordinary files (a stock ISO, a rendered `answer.toml`, and the
prepared ISO it produces).

## Why a stock ISO does not do this on its own

A stock Proxmox VE ISO's GRUB menu **waits forever** for a human to pick an
entry. Its `grub.cfg` contains, verbatim:

```
if [ -f auto-installer-mode.toml ]; then
    set timeout-style=menu
    set timeout=10
    menuentry 'Install Proxmox VE (Automated)' ... linux /boot/linux26 ... proxmox-start-auto-installer
fi
```

The `timeout` setting and the "Automated" menu entry only exist **inside that
`if`** — they are conditioned on a file, `auto-installer-mode.toml`, that a
stock ISO does not carry. Without it, GRUB never sets a timeout at all, so
the menu blocks indefinitely. This was verified on real hardware: a stock
ISO attached over this collection's virtual-media path sat at that menu with
no timeout, and the operator watching the console confirmed it visually. It
is not a matter of a short timeout being easy to miss — there genuinely is no
timeout on a stock image.

Proxmox's own `proxmox-auto-install-assistant` tool (and this role's
container fallback, when the vendor tool is unavailable) adds that file, plus
the `answer.toml` the installer reads once it takes the automated path. That
is the entire mechanism this role automates.

## Two implementation paths

### 1. `proxmox-auto-install-assistant` (preferred, used automatically when present)

Proxmox's own supported tool. When found on the controller's `PATH` (as
`asmb8_autoinstall_iso_vendor_tool_path`, default
`proxmox-auto-install-assistant`), this role:

1. Renders `answer.toml` from the `asmb8_autoinstall_iso_answer_*` variables.
2. Runs `proxmox-auto-install-assistant validate-answer` against it and
   **refuses to continue if validation fails.**
3. Runs `proxmox-auto-install-assistant prepare-iso <source> --fetch-from iso
   --answer-file answer.toml --output <output>`.

`--fetch-from iso` bakes the answer file straight into the prepared ISO, so
the resulting image needs no network fetch and no extra infrastructure (no
HTTP server, no PXE/DHCP/TFTP) at install time — consistent with this
collection's whole reason for existing.

### 2. Container-based `xorriso` rebuild (fallback)

Used only when the vendor tool is not available (or
`asmb8_autoinstall_iso_prefer_vendor_tool: false` is set while a container
runtime is). Runs `xorriso` inside a container (`docker` or `podman`,
`asmb8_autoinstall_iso_container_runtime`) built from a plain Debian/Ubuntu
image (`asmb8_autoinstall_iso_container_image`, default
`debian:bookworm-slim`) with `xorriso` installed at run time — there is no
pre-built image assumption. The recipe:

```
xorriso -indev in.iso -outdev out.iso -boot_image any replay \
        -compliance no_emul_toc \
        -map auto-installer-mode.toml /auto-installer-mode.toml \
        -map answer.toml /answer.toml \
        -commit
```

is the same El Torito-preserving `-boot_image any replay` recipe that was
independently verified to survive a real Proxmox VE ISO rebuild (both the
BIOS and UEFI boot images, and the boot catalog, all present afterwards, at
shifted LBAs) — extended with the two `-map` entries that actually add the
files that make the ISO auto-install. See
[`docs/proxmox-autoinstall.md`](/docs/proxmox-autoinstall.md) for exactly
what evidence exists for each path — **only the vendor tool path has real
evidence behind it beyond "the ISO structure survives a rebuild".** The
fallback path adds a defensive check before touching `grub.cfg`: it inspects
the stock ISO's own `grub.cfg` for the same `auto-installer-mode.toml`
conditional quoted above, and only patches `grub.cfg` (using
`asmb8_autoinstall_iso_grub_cfg_override`, if you supply one) when that
conditional is missing — it will not guess a GRUB patch for a menu layout it
has not seen, and fails with an actionable message instead.

**If neither tool is available, this role refuses to produce an ISO at
all.** It will not silently emit an unprepared, stock-equivalent ISO that
looks like a successful run but boots straight into the same indefinite
GRUB wait described above.

## Disk safety

This is the most important section of this document.

An auto-install `answer.toml` names a disk (or disks) and wipes it/them with
**no confirmation prompt** the moment the target machine boots the prepared
ISO. There is no "are you sure?" — that is the entire point of an
unattended installer.

This role therefore:

- **Never defaults a target disk.** `asmb8_autoinstall_iso_answer_disk_list` and
  `asmb8_autoinstall_iso_answer_disk_filter` both default to empty, and this role
  asserts that exactly one of them is set (never both, never neither) before
  it will render anything. See `tasks/validate.yml`.
- **Refuses a vague target.** Each guard rejects an empty entry, a
  glob/wildcard character (`*`, `?`, `[`, `]`), a leading `/dev/` path (the
  schema wants bare kernel names, see below), and "first available"-style
  placeholders (`first`, `any`, `all`, `auto`, `default`, `primary`,
  `largest`, `smallest`, case-insensitively).
- **Requires a separate, explicit destructive-confirmation flag**:
  `asmb8_autoinstall_iso_confirm_destructive: true`. This is the same name
  and gating pattern as this collection's `asmb8_baremetal_install` role's
  own `asmb8_baremetal_install_confirm_destructive` — never defaulted
  `true`, checked with the `bool` filter (so `true`, `yes`, `"true"`, and
  `-e asmb8_autoinstall_iso_confirm_destructive=true` on the command line
  all count, exactly like the sibling role), and never satisfied by simply
  leaving it unset.

### `disk_list` vs. `disk_filter` — and why kernel names are not enough

Proxmox's `disk-setup.disk-list` field takes **bare kernel device names**
(e.g. `nvme0n1`, `sda` — not `/dev/nvme0n1`, and not a `/dev/disk/by-id/...`
path; that field is documented to take short kernel names only). That is a
real limitation, not a design choice of this role: **kernel device names are
not guaranteed stable across reboots**, especially on a board with more than
one storage controller, where enumeration order can depend on discovery
timing rather than anything physical about the drive. Pointing an unattended
installer at the wrong disk is unrecoverable — there is no prompt to catch
the mistake, and no "undo" once the write starts.

`asmb8_autoinstall_iso_answer_disk_filter` is Proxmox's actual stable-identifier
mechanism for this field: a table of udev-property name/value pairs (e.g.
`{"ID_SERIAL_SHORT": "S3Z8NB0M123456"}`) that the installer matches against
the real hardware at install time, regardless of what kernel name the disk
happens to enumerate as on that particular boot. **Prefer `disk_filter` over
`disk_list` whenever the target board supports it.**

Either way: **do not guess a serial or a kernel name.** Boot the target
board (or a live environment on it) and run
`proxmox-auto-install-assistant device-info` against it to read its actual
udev properties before writing either variable. If you cannot run that tool
against the specific board yet, treat this role's disk-targeting variables
as unset — deliberately erroring out — rather than filling in something you
have not verified against that exact machine.

## Generating a password hash

Prefer `asmb8_autoinstall_iso_answer_root_password_hashed` over
`asmb8_autoinstall_iso_answer_root_password` so a plaintext password never has
to exist in a playbook or a vaulted variable file:

```console
$ mkpasswd -m sha-512
Password: ********
$6$rounds=656000$........................$........................................................
```

(`mkpasswd` ships in the `whois` package on Debian/Ubuntu, `expect` on some
other distributions.) Paste the full output as
`asmb8_autoinstall_iso_answer_root_password_hashed`. This role asserts exactly
one of the two password variables is set — never both, never neither — and
`no_log`s the task that renders `answer.toml` so neither ever appears in a
play recap, `--diff` output, or a log.

## Observing an install in progress: serial console

The stock ISO's GRUB already attempts a serial console
(`serial --unit=0 --speed=115200`, appended to `terminal_input`/
`terminal_output`) and offers an `Install Proxmox VE (Terminal UI, Serial
Console)` entry (`proxtui console=ttyS0,115200`). In principle this gives a
way to watch an unattended install progress over IPMI Serial-over-LAN
without needing the KVM/media channel at all.

**In practice, on the board this role was developed against, that path did
not produce output**: an SOL session opened successfully but received no
data. The likely cause is that BIOS console redirection is not enabled for
that board — SOL only carries what the BIOS/firmware is configured to send
to the serial port, and this collection has not yet found a way to enable
that setting on this board. Do not assume SOL will show you anything on
similar hardware until you have separately confirmed console redirection is
on.

## Expected duration

The Proxmox VE 9.2-1 installer squashfs images this role was developed
against are large: `pve-installer.squashfs` is 614 MB, `pve-base.squashfs`
is 108 MB. Streamed over this collection's virtual-media path at the ~800
KB/s measured in testing, a real install streams for **13+ minutes** before
the installer even finishes loading, before disk partitioning or package
installation begin. Do not treat a long-running install as failed and kill
it — check timestamps, IPMI power/activity, or (if you have console
redirection enabled — see above) the serial console before intervening.

## Honesty about what is and is not proven

**Nothing in this role has completed a full unattended install on real
hardware yet.** The `xorriso -boot_image any replay` recipe has been proven
to preserve a stock ISO's El Torito boot structure through a rebuild; the
vendor tool path has real evidence behind its underlying mechanism
(Proxmox's own documented behaviour); but no run of either path in this role
has yet been watched end-to-end to a completed, running Proxmox VE install
on the actual target hardware. Treat every claim above about what a
prepared ISO *should* do as exactly that — a should, backed by the cited
evidence — not as an observed result, until this note is updated to say
otherwise.

## Variables

See [`defaults/main.yml`](defaults/main.yml) for the complete, commented
list, including per-key confidence notes on the `answer.toml` schema
(most keys are directly attested against Proxmox's own documentation; a
few, marked accordingly, are this role's best-effort rendering of something
not independently confirmed). The variables that matter most:

| Variable | Required | Notes |
| --- | --- | --- |
| `asmb8_autoinstall_iso_source` | yes | Path to the stock ISO. |
| `asmb8_autoinstall_iso_output` | yes | Path to write the prepared ISO. |
| `asmb8_autoinstall_iso_confirm_destructive` | yes | Must be boolean `true`. See "Disk safety". |
| `asmb8_autoinstall_iso_answer_disk_list` **or** `asmb8_autoinstall_iso_answer_disk_filter` | exactly one | No default. See "Disk safety". |
| `asmb8_autoinstall_iso_answer_disk_filesystem` | yes | One of `ext4`, `xfs`, `zfs`, `btrfs`. |
| `asmb8_autoinstall_iso_answer_fqdn` | yes | No default. |
| `asmb8_autoinstall_iso_answer_timezone` | yes | No default. |
| `asmb8_autoinstall_iso_answer_root_password_hashed` **or** `_root_password` | exactly one | Prefer the hashed form — see above. |

## Example

```yaml
- hosts: localhost
  gather_facts: false
  roles:
    - role: james_crowley.asmb8_ikvm.asmb8_autoinstall_iso
      vars:
        asmb8_autoinstall_iso_source: /srv/isos/proxmox-ve_9.2-1.iso
        asmb8_autoinstall_iso_output: /srv/isos/proxmox-ve_9.2-1-auto.iso

        # Verified against the real board with `proxmox-auto-install-assistant
        # device-info` before being written here -- see "Disk safety".
        asmb8_autoinstall_iso_answer_disk_filter:
          ID_SERIAL_SHORT: "S3Z8NB0M123456"
        asmb8_autoinstall_iso_confirm_destructive: true

        asmb8_autoinstall_iso_answer_fqdn: pve-lab-01.example.invalid
        asmb8_autoinstall_iso_answer_mailto: root@example.invalid
        asmb8_autoinstall_iso_answer_timezone: Etc/UTC
        asmb8_autoinstall_iso_answer_root_password_hashed: "{{ vaulted_root_password_hash }}"

        asmb8_autoinstall_iso_answer_disk_filesystem: zfs
        asmb8_autoinstall_iso_answer_disk_zfs:
          raid: raid0
```

Feed `asmb8_autoinstall_iso_output` to whatever role or module attaches an
ISO to the BMC's virtual media (this collection's `asmb8_media`, driven by
`asmb8_baremetal_install`) and arms a one-time boot to it.
