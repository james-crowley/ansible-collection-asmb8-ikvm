<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Proxmox VE automated installation: ISO preparation

This is the evidence and reference document behind the
[`asmb8_autoinstall_iso`](/roles/asmb8_autoinstall_iso/) role. The role's own
[README](/roles/asmb8_autoinstall_iso/README.md) is the operational
guide — read it first, especially "Disk safety". This document exists to
record *why* the role is built the way it is, dated and falsifiable, in the
same spirit as this collection's `docs/hardware-evidence-2026-08-08.md`.

> **This role has not yet completed a full unattended install on real
> hardware.** Everything below is either (a) something directly observed
> against a real Proxmox VE 9.2-1 ISO and/or real hardware, cited as such, or
> (b) documentation-derived and marked with its confidence level. Nothing
> here should be read as "and then the install succeeded" unless it says so
> explicitly, and as of this writing nothing does.

## Fact 1: a stock ISO's GRUB menu waits forever

Inspecting a stock Proxmox VE 9.2-1 ISO's `/boot/grub/grub.cfg` directly
shows:

```
if [ -f auto-installer-mode.toml ]; then
    set timeout-style=menu
    set timeout=10
    menuentry 'Install Proxmox VE (Automated)' ... linux /boot/linux26 ... proxmox-start-auto-installer
fi
```

The `timeout` directive and the `Install Proxmox VE (Automated)` menu entry
exist **only inside this conditional**, gated on a file
(`auto-installer-mode.toml`) that does not exist at the root of a stock ISO.
Outside that `if`, nothing else in the file sets a timeout. The practical
consequence: a stock ISO's GRUB menu has **no timeout configured at all**,
and sits at the menu indefinitely.

**Verified on real hardware, 2026-08-08**: a stock ISO attached over this
collection's iUSB virtual-media path was booted on the target lab machine.
The host sat at the GRUB menu with no timeout; the console was watched
directly and confirmed no progression occurred. This was not "the timeout
was short and got missed" — there was no timeout to miss.

This is the entire reason `asmb8_autoinstall_iso` exists: adding
`auto-installer-mode.toml` (and the `answer.toml` it makes the installer look
for) to the ISO root is what activates the branch above.

## Fact 2: the `auto-installer-capable` marker

The ISO root also carries a zero-byte file, `auto-installer-capable`, present
on the stock image. Its existence signals that the image *supports*
auto-install once prepared — it is not itself sufficient (see Fact 1: the
GRUB conditional above checks for `auto-installer-mode.toml` specifically,
not for this marker).

## Fact 3: a serial-console path exists, but console redirection was not observed to work on the test board

The same `grub.cfg` already attempts a serial console:

```
serial --unit=0 --speed=115200
```

appended to both `terminal_input` and `terminal_output`, and offers a
dedicated menu entry, `Install Proxmox VE (Terminal UI, Serial Console)`,
which boots with `proxtui console=ttyS0,115200`. In principle this is a way
to observe an install's progress over IPMI Serial-over-LAN (SOL) without
needing the KVM/virtual-media channel at all.

**In practice**: an SOL session against the target board opened
successfully but received no data whatsoever, on either the serial-console
installer entry or otherwise. The most likely explanation is that BIOS/UEFI
console redirection to the serial port is not enabled on this board's
firmware configuration — SOL only relays what the platform firmware is
configured to send to the UART, and firmware that never writes to the serial
port produces a silent, but successfully-open, SOL session, which is
consistent with what was observed. This has not been independently
confirmed by finding and toggling the relevant firmware setting; it is the
most likely explanation given the observed symptom, not a confirmed root
cause. Do not assume SOL will show output on similar hardware without
separately verifying console redirection is enabled.

## Fact 4: El Torito survives an `xorriso -boot_image any replay` rebuild

The stock ISO's boot structure, read directly with `xorriso
-report_el_torito`:

- Boot catalog at LBA 4660.
- BIOS boot image `/boot/grub/i386-pc/eltorito.img` at LBA 6025, with
  `boot-info-table grub2-boot-info`.
- UEFI boot image `/efi.img` at LBA 156.
- Hybrid image: `MBR protective-msdos-label grub2-mbr cyl-align-off GPT APM`.
- Two GRUB configs exist: `/boot/grub/grub.cfg` (the real menu, quoted in
  Fact 1) and `/efi/boot/grub.cfg` (module loading only — it does not carry
  its own menu).

Rebuilding the ISO with:

```
xorriso -indev in.iso -outdev out.iso -boot_image any replay \
        -compliance no_emul_toc -map /local/grub.cfg /boot/grub/grub.cfg -commit
```

inside a plain `debian:bookworm-slim` container (with `xorriso` installed at
run time via `apt-get`) was verified, on 2026-08-08, to preserve the El
Torito structure through the rebuild: the boot catalog and both boot images
(BIOS and UEFI) were present and correctly typed afterwards, at shifted LBAs
consistent with the file layout changing. This is the recipe
`asmb8_autoinstall_iso`'s container fallback path
(`templates/container_prepare.sh.j2`) is built on, extended with two more
`-map` entries that add `auto-installer-mode.toml` and `answer.toml` at the
ISO root — those two additions are what actually change installer behaviour;
the recipe above by itself only proves the rebuild mechanism does not corrupt
the ISO's ability to boot at all.

**What this does and does not prove**: it proves the rebuild mechanism is
structurally sound. It does **not** prove the resulting ISO completes an
unattended install — that has not yet been observed end-to-end on real
hardware through either preparation path. See the role README's "Honesty
about what is and is not proven".

### Docker on macOS as the supported path for this recipe

`xorriso`, `mkisofs`/`genisoimage`, and `7z` are not installed natively on
the controller this role was developed on (macOS); Docker 29.6 is. A Linux
controller may well have native `xorriso` or `proxmox-auto-install-assistant`
available directly — this role does not require a container on Linux, it
only falls back to one when the vendor tool is absent, regardless of
platform.

## Fact 5: expect a 13+ minute install, not a quick one

`pve-installer.squashfs` is 614 MB and `pve-base.squashfs` is 108 MB on the
Proxmox VE 9.2-1 ISO this role was developed against. Streamed over this
collection's iUSB virtual-media path at the ~800 KB/s measured in testing, a
real install streams for **13+ minutes** before the installer environment
even finishes loading — before disk partitioning or package installation
begin. Any timeout logic built around this role's output (health checks,
CI approval gates, "did it hang" heuristics) needs to reflect that duration,
not a PXE-speed assumption.

## The `answer.toml` schema: what this role is confident about, and what it is not

Proxmox migrated the schema's key spelling from snake_case to kebab-case
starting with PVE 8.4-1/PBS 3.4-1; a `pve-devel` patch from April 2025
("assistant: validate: warn if answer file contains old snake_case keys")
adds a `validate-answer` warning for the old spelling and lists conversions
including `root_password` &rarr; `root-password`, `disk_list` &rarr;
`disk-list`, and `filter_match` &rarr; `filter-match`. The old snake_case
spelling still parses on current Proxmox VE (that patch is a warning, not a
rejection), but is deprecated. **This role's `answer.toml.j2` template
always emits the current kebab-case spelling**, because the ISO this role is
built against is Proxmox VE 9.2-1.

Per-section confidence:

| Section | Keys | Confidence |
| --- | --- | --- |
| `[global]` | `keyboard`, `country`, `fqdn`, `mailto`, `timezone`, `root-password`, `root-password-hashed`, `reboot-on-error`, `reboot-mode` | High — directly attested in Proxmox's own Automated Installation documentation and cross-checked against a real-world example `answer.toml` and the kebab-case migration patch above. |
| `[network]` | `source`, `cidr`, `dns`, `gateway` | High — same sourcing as `[global]`. |
| `[network.filter]` / `[disk-setup.filter]` (`filter`, `filter-match`) | High for the `disk-setup` form (also named directly in the migration patch); **the exact set of udev-property keys the *network* filter inspects is not independently confirmed** — treat any specific property name used there as something to verify with `proxmox-auto-install-assistant device-match`, not as guaranteed. | Medium (network filter specifically) |
| `[disk-setup]` | `filesystem`, `disk-list` | High. |
| `zfs.*` | `raid`, `ashift`, `arc-max`, `checksum`, `compress`, `copies`, `hdsize` | High — directly attested, and `arc-max`'s kebab-case spelling specifically is named in the migration patch. |
| `lvm.*` | `hdsize`, `swapsize`, `maxroot`, `maxvz`, `minfree` | High. |
| `btrfs.*` | `raid`, `hdsize`, `compress` | High. |
| `[global]` `fqdn` as an object (`{source = "from-dhcp", domain = ...}`) | Documented to exist. | **Not implemented** by this role's template — only the plain-string form is rendered. Said so rather than silently ignored: if you need DHCP-derived FQDN, this role's template does not currently support it. |
| `root-ssh-keys`, `subscription-key`, network interface-name pinning, the post-install webhook section, the first-boot-hook section | Documented to exist in the wider schema. | **Out of scope for this role's template** — not rendered at all, by choice, not by oversight. Extend `templates/answer.toml.j2` if you need one of these; do not assume this role covers the entire schema. |
| The exact byte-for-byte content `proxmox-auto-install-assistant` itself writes to `auto-installer-mode.toml` | Reconstructed from public inspection of the tool's `AutoInstSettings` struct (`mode`, `partition_label`, `http.{url,cert_fingerprint,token}`), not from a captured real output file. | **Unverified** — only relevant to the container fallback path; the vendor tool path never needs this role to write this file at all, because the tool writes its own. See `templates/auto_installer_mode.toml.j2`. |

Whenever this table and Proxmox's own tooling disagree, **trust the
tooling**: `tasks/render_answer.yml` always runs
`proxmox-auto-install-assistant validate-answer` against the rendered file
when the tool is present, and refuses to proceed if it reports the file
invalid.

## `proxmox-auto-install-assistant` command reference (as used by this role)

```console
$ proxmox-auto-install-assistant validate-answer answer.toml
$ proxmox-auto-install-assistant prepare-iso source.iso \
      --fetch-from iso --answer-file answer.toml --output prepared.iso
```

`--fetch-from iso` bakes the answer file into the ISO itself, so nothing
else needs to serve it at install time. The tool also supports
`--fetch-from http|partition|pxe` for setups that want the answer file
fetched at boot instead of baked in — **this role only ever uses
`--fetch-from iso`**, by design, because avoiding any extra install-time
infrastructure is this whole collection's point.

## Disk safety

See the role README's ["Disk safety"](/roles/asmb8_autoinstall_iso/README.md#disk-safety)
section for the full explanation and the exact guard behaviour. In short:
there is no default target disk anywhere in this role, both a wildcard/
placeholder target and an unset target are asserted against and refused, and
a separate `asmb8_autoinstall_iso_confirm_destructive: true` is
required before this role will render anything, exactly mirroring how this
collection's connection doc fragment gates unencrypted BMC transport.

## Sources consulted

- Proxmox VE Automated Installation wiki page:
  <https://pve.proxmox.com/wiki/Automated_Installation>
- `pve-devel` patch, April 2025: "assistant: validate: warn if answer file
  contains old snake_case keys" (kebab-case migration, PVE 8.4-1/PBS 3.4-1).
- A real-world example `answer.toml`
  (`FreddyFunk/Proxmox-VE-Auto-Installer`, `assets/answer.toml`) used to
  cross-check the schema's key spellings against an actual working file
  rather than documentation prose alone.
- Public inspection of `proxmox-auto-install-assistant`'s `main.rs`
  (`proxmox/pve-installer`) for the `AutoInstSettings` struct referenced in
  the schema-confidence table above.
- Direct inspection of a real Proxmox VE 9.2-1 ISO (`grub.cfg`,
  `auto-installer-capable`, El Torito structure via `xorriso
  -report_el_torito`) and of `pve-installer.squashfs`/`pve-base.squashfs`
  file sizes, 2026-08-08.
- Live hardware observation of the stock-ISO GRUB hang, the iUSB streaming
  rate, and the empty SOL session, 2026-08-08 — see this collection's
  `docs/hardware-evidence-2026-08-08.md` for the broader hardware-evidence
  record this role's facts are drawn from.
