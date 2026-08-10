<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Netboot design: getting the bulk of the Proxmox VE 9 installer off iUSB

This is desk research, done from public documentation and public source code.
**No request was made to any BMC or lab host while writing this.**
Everything below is either a claim sourced to a
URL (a wiki page, a manual page, a piece of upstream source read directly),
something measured directly against a public download (marked as such, with
a date), or an explicit inference marked as one. Where this document could
not confirm something, it says "unverified" rather than guessing — this
collection has been burned by confident-but-wrong diagnoses before (see
`docs/hardware-evidence-2026-08-08.md`'s "READ TOC" story), and an honest
"unknown" here is worth more than a plausible-sounding kernel parameter that
turns out to be fiction.

## Why this document exists

`docs/hardware-evidence-2026-08-08.md` measured this board's iUSB virtual-CD
channel at **~790–900 KB/s**, correctly windowed over the bulk-streaming
phase. `docs/proxmox-autoinstall.md` ("Fact 5") and both role READMEs already
draw the consequence: streaming a 614 MB `pve-installer.squashfs` alone costs
**13+ minutes**, and the full 1,628 MiB ISO used in that testing is well over
an hour, with at least one run failing outright with I/O errors before this
collection's `READ TOC` fix landed. The bottleneck is structural, not a bug:
USB Mass Storage over this BMC's iUSB relay is strictly serial — one SCSI
command outstanding at a time, ~32 KB per `READ(10)`, ~30 ms per round trip —
and no amount of client-side tuning fixes that (this collection tried
`TCP_NODELAY` and measured a null result, see the hardware-evidence doc's
final section).

The fix this document works out: attach only a **small bootstrap image
(≤16 MiB budget)** over iUSB. That bootstrap's only job is to bring up the
target's real NIC and pull the actual Proxmox installer over plain HTTP, at
LAN speed, completely bypassing the BMC for the bulk transfer. This document
is the research behind that design — it does not implement it. Nothing in
`roles/` or `plugins/` currently does what is described here.

**Read `docs/hardware-evidence-2026-08-08.md` and `docs/proxmox-autoinstall.md`
first** — this document assumes their findings (the ~800 KB/s figure, the
`auto-installer-mode.toml` GRUB gate, the `proxmox-auto-install-assistant
prepare-iso --fetch-from iso` mechanism this collection's
`asmb8_autoinstall_iso` role already uses) and extends them rather than
repeating them.

---

## 1. Recommended design, in brief

1. **Keep using `proxmox-auto-install-assistant`**, Proxmox's own tool
   (already the preferred path in `asmb8_autoinstall_iso`), but pass it
   `--pxe --pxe-loader ipxe` in addition to the answer-file flags this
   collection already uses. This produces `vmlinuz`, `initrd.img`, a
   generated `boot.ipxe`, and a copy of the source ISO with its `/boot`
   directory stripped (all four described exactly, with citations, in §3).
   Serve that output directory over plain HTTP from anywhere on the LAN —
   the Ansible controller itself, with `python3 -m http.server`, is enough.
2. **Do not use the generated `boot.ipxe` verbatim.** It opens with an
   unconditional `dhcp` command (§3, §5) — exactly the standing-DHCP
   dependency this project has rejected. Replace only its first two lines
   with static `set net0/ip …` / `ifopen net0` commands (exact syntax in
   §5); the rest of the generated script (the `kernel`/`initrd`/`boot`
   sequence) is a small, static, human-readable text file and can be reused
   as-is or trivially templated by Ansible.
3. **Package that script behind a small, prebuilt iPXE, with no compiler
   involved.** Download the prebuilt `ipxe.lkrn` binary once (a static
   ~382 KiB file, cacheable — see §4), and wrap it plus the static script in
   a tiny GRUB-based bootable ISO using `grub-mkrescue` (already a standard
   Debian/Ubuntu package, the same category of tool this collection already
   uses `xorriso` from — see §4 for exactly how GRUB loads `ipxe.lkrn` and
   its script with zero compilation). Attach *that* tiny ISO over iUSB
   instead of the 1.6 GiB installer ISO.
4. **`sanboot` is not part of this design, and should not be.** The concern
   raised in the brief is real: iPXE's own community confirms there is "no
   bridge from PXE to protected kernel" once the OS kernel takes over (§2).
   Proxmox's own `--pxe` mechanism avoids the problem entirely by never using
   `sanboot` — it loads the *entire* installer ISO into RAM as a second
   Linux `initrd` segment before the kernel ever starts (§2, §3). This
   document recommends following that same pattern, not working around
   `sanboot`'s limitation with a different trick.

The net effect: iUSB carries only the bootstrap ISO (single-digit MiB).
Everything Proxmox-sized — `vmlinuz`, `initrd.img`, and the 1.5+ GiB stripped
installer ISO — travels over HTTP on the LAN, at whatever speed the target's
real NIC and the serving host can sustain, not at ~800 KB/s.

---

## 2. The `sanboot` question — verdict

**`sanboot`-ing an HTTP-hosted ISO does not survive the handoff to a Linux
kernel that needs to keep reading that ISO afterward. This is a real,
structural limitation, not a configuration mistake, and Proxmox's own
official netboot mechanism avoids it entirely by not using `sanboot` at
all.**

### What `sanboot` actually is

iPXE's own command reference states plainly: `sanboot` boots from a SAN
target by attaching it as a BIOS drive and issuing an INT 13 boot; "It is
generally impossible for this command to return successfully, since if the
boot is successful then control will not return to iPXE" — the CD/disk is
emulated through the BIOS INT13 interface
([ipxe.org/cmd/sanboot](https://ipxe.org/cmd/sanboot)). That interface is a
**16-bit, real-mode BIOS calling convention**. iPXE's own runtime — which is
what actually answers those INT13 calls, translating them into network I/O —
lives in memory the BIOS/real-mode environment can reach.

### Why the Linux kernel breaks it

A Linux kernel booted this way starts in real mode (to make the initial INT13
calls that load itself and its initrd), but switches to protected/long mode
during early boot and takes over its own hardware access from that point on.
Once that happens, INT13 is no longer how Linux does disk I/O — it is not a
call Linux issues at all outside the earliest bootstrap. A device that only
exists as an emulated INT13 vector, with no real hardware protocol behind it,
has nothing for any post-boot Linux driver to attach to.

This is exactly what an iPXE maintainer says directly, discussing this exact
failure mode: **"sanboot is primar[il]y made for iSCSI which uses the iBFT
ACPI table … to hand over the connection to the data, this standard is for
iSCSI only" and, on the general case, "ISO is not fully loaded into ram &
there's no 'bridge' from pxe to protected kernel, so after loosing network
stack (switching kernel), you cannot access iso anymore"**
([github.com/ipxe/ipxe discussion #912](https://github.com/ipxe/ipxe/discussions/912),
user `NiKiZe`). iSCSI is the one case with a real escape hatch, because Linux
has its own native iSCSI initiator that can pick up the *same session*
independently of iPXE's BIOS-call emulation — there is no equivalent native
Linux driver for "a SAN device that only exists as an iPXE-maintained INT13
trap." A separate iPXE mailing-list thread on "sanbooting FreeBSD ISO under
UEFI" confirms the same problem recurs on the UEFI side of the interface too
([lists.ipxe.org, 2017](https://lists.ipxe.org/pipermail/ipxe-devel/2017-January/005436.html)).

The same discussion #912 also names the community's own workaround: **load
the whole ISO into RAM as an initrd, then hand it to `memdisk`** — because
once the entire image is a plain block of memory rather than something
reachable only through a BIOS trap, there is no handoff problem left to
solve. `memdisk` (from `syslinux`) is itself "a BIOS-only implementation
which requires INT13 and cannot work in EFI mode," per the same discussion —
so even that workaround is bounded to legacy BIOS.

### Why this project's own iUSB path is not the same thing, and does not validate `sanboot`

`docs/hardware-evidence-2026-08-08.md` records Linux successfully reading a
CD *after* kernel handoff over this project's own iUSB channel (the `READ
TOC` bug was found precisely because Linux issued a real `TEST UNIT
READY`/`READ TOC` sequence post-boot). That is not a counterexample to the
`sanboot` finding above — it is a different mechanism entirely. The BMC's
iUSB channel presents a **real emulated USB Mass Storage device**, which
Linux's `usb-storage` kernel driver talks to directly over USB, independently
of the BIOS. There is a genuine hardware protocol (USB) underneath it that a
native Linux driver can attach to after protected-mode handoff, which is
exactly the thing an iPXE HTTP-`sanboot`'d ISO does not have.

### What Proxmox's own mechanism does instead

Confirmed directly from `proxmox-auto-install-assistant`'s own source (§3):
its `--pxe` output never calls `sanboot`. It has iPXE fetch the kernel and
initrd as ordinary files over HTTP (`kernel`/`initrd` commands, not
`sanboot`), and it has iPXE fetch the **entire installer ISO** as a second,
raw-named `initrd` segment — i.e. exactly the "load the whole thing into RAM
first" pattern the iPXE community above independently arrived at as the fix
for `sanboot`'s limitation. This is strong, convergent evidence that the
limitation is real (an independent community thread and Proxmox's own
shipped tooling arrived at the same workaround for the same underlying
problem) and that Proxmox's mechanism, not `sanboot`, is the right model to
follow.

**Bottom line for this project: do not `sanboot` the Proxmox ISO. Use
kernel+initrd netboot with the ISO injected as a raw initrd segment, exactly
as Proxmox's own tooling already does.**

---

## 3. Proxmox VE 9's own netboot support — the mechanics

### It exists, it is recent, and it version-matches the ISO this project already tests against

`proxmox-auto-install-assistant`'s PXE/iPXE support first appears in the
`pve-installer` package changelog at **version 9.1.7, dated 2026-03-31**
(read directly from `debian/changelog` in the upstream repository,
[github.com/proxmox/pve-installer](https://raw.githubusercontent.com/proxmox/pve-installer/master/debian/changelog)):
*"assistant: add support for splitting ISO into (i)PXE-compatible files,
extracting vmlinuz and initrd for network booting. A configuration file for
iPXE can optionally be generated via `--pxe-loader`."* Public commentary
places this feature as shipping with Proxmox VE 9.2. `docs/hardware-evidence-2026-08-08.md`
and `docs/proxmox-autoinstall.md` both develop this collection against a
**Proxmox VE 9.2-1** ISO — the feature described below is not a future
Proxmox release this project would need to wait for; it is present in the
exact ISO version already in use.

### The command

The official wiki, [pve.proxmox.com/wiki/Automated_Installation](https://pve.proxmox.com/wiki/Automated_Installation),
documents it directly (quoted as fetched):

> "Instead of writing the prepared ISO to a physical medium, you can boot
> the automatic installer over the network via PXE. Pass `--pxe` to split
> the prepared image into the `vmlinuz` kernel and `initrd.img` files needed
> for network booting, instead of producing an ISO file. In this mode,
> `--output` must point to a *directory* to place the files in, rather than
> a file name."
>
> "To additionally generate a configuration snippet for a specific loader,
> pass `--pxe-loader`. Currently the only supported value is `ipxe`;
> specifying it implies `--pxe`."

The wiki's own example:

```
proxmox-auto-install-assistant prepare-iso /path/to/source.iso \
--fetch-from http \
--url "https://10.0.0.100/answer" \
--pxe \
--output /srv/tftp/proxmox-auto/
```

This project's existing convention (`docs/proxmox-autoinstall.md`,
`asmb8_autoinstall_iso`) already prefers `--fetch-from iso` — baking the
answer file into the ISO — specifically to avoid needing a second piece of
install-time infrastructure. That preference composes cleanly with `--pxe`:
read directly from `proxmox-auto-install-assistant`'s own CLI validation
logic (`proxmox-auto-install-assistant/src/main.rs`, fetched from
[github.com/proxmox/pve-installer](https://raw.githubusercontent.com/proxmox/pve-installer/master/proxmox-auto-install-assistant/src/main.rs)),
there is **no restriction preventing `--fetch-from iso` from being combined
with `--pxe`/`--pxe-loader ipxe`** — the only fetch-mode-specific checks are
that `--url`/`--cert-fingerprint` require `--fetch-from http`, and that
`--answer-file` requires `--fetch-from iso`. The recommended command for this
project's use case is therefore:

```
proxmox-auto-install-assistant prepare-iso proxmox-ve_9.2-1.iso \
  --fetch-from iso --answer-file answer.toml \
  --pxe --pxe-loader ipxe \
  --output /srv/http/proxmox-auto/
```

This has **not been run in this research** (no compiler/vendor tool was
invoked; this is read from source, not executed) — treat the exact output
filenames below as sourced from code, not from an observed directory
listing.

### What that produces, and how the ISO's bulk gets fetched over HTTP

`proxmox-auto-install-assistant/src/main.rs`'s PXE-generation function
(`prepare_pxe_compatible_files`, read directly from the same GitHub source
as above):

- Extracts `/boot/linux26` from the source ISO to `vmlinuz`.
- Extracts `/boot/initrd.img` to `initrd.img`.
- **Removes the whole `/boot` folder from the (still-present) ISO copy** —
  the source comment reads, verbatim: *"remove the whole /boot folder from
  the ISO to save some space (nearly 100 MiB), as it is unnecessary with
  PXE."* The remainder of the ISO — including both installer squashfs images
  (`pve-installer.squashfs`, `pve-base.squashfs`) and, when `--fetch-from
  iso` was used, the embedded `answer.toml`/`auto-installer-mode.toml` — is
  otherwise untouched and is what gets served as the third file.
- Generates `boot.ipxe` (when `--pxe-loader ipxe` is given) from this literal
  template (quoted verbatim from source):

  ```
  #!ipxe

  dhcp

  menu Welcome to {product_name} {release}-{isorelease}
  {menu_items}
  choose --default auto --timeout 10000 target && goto ${target}

  {menu_options}
  :load
  initrd initrd.img
  initrd {iso_filename} proxmox.iso
  boot
  ```

  with each menu option expanding to a `kernel vmlinuz {DEFAULT_KERNEL_PARAMS}
  initrd=initrd.img {boot_option_params}` line before `goto load`.
  `DEFAULT_KERNEL_PARAMS` is the literal constant `"ramdisk_size=16777216 rw
  quiet"`; per-option suffixes seen in source include `splash=silent
  proxmox-start-auto-installer` (the unattended entry this project cares
  about), `splash=silent proxmox-tui-mode`, and `splash=verbose
  proxmox-debug`; architecture-specific console parameters
  (`console=ttyS0,115200`, or `console=ttyAMA0,115200 console=ttyS0,115200`
  on arm64) are appended by the same function. **The full concatenated
  string for the automated-install entry (e.g. whether `vga=788` is included
  by default) was reconstructed from these source fragments, not captured as
  a literal tool-generated file — treat the exact final line as inferred,
  not verbatim-verified,** though every individual fragment quoted above is
  read directly from source.

- **Crucially: all three URIs (`vmlinuz`, `initrd.img`, `{iso_filename}`) are
  bare, relative filenames, not absolute URLs.** iPXE resolves relative URIs
  against the location the *script itself* was loaded from
  ([ipxe.org scripting conventions](https://ipxe.org/scripting) — general
  iPXE behavior for relative URIs). Practically: if `boot.ipxe` is served
  from `http://server/proxmox-auto/boot.ipxe`, then `kernel vmlinuz …`,
  `initrd initrd.img`, and `initrd proxmox-ve_9.2-1.iso proxmox.iso` all
  resolve to `http://server/proxmox-auto/<name>` automatically — **plain
  HTTP end to end, no TFTP required anywhere in this chain**, provided the
  script itself is also fetched over HTTP (see §4 for how the bootstrap gets
  iPXE to that script's URL in the first place).

### The `initrd <uri> <name>` mechanism — how "the ISO" becomes a file the installer can see

The second `initrd` line — `initrd {iso_filename} proxmox.iso` — is iPXE's
own documented mechanism for injecting an arbitrary fetched file into the
Linux kernel's initramfs under a chosen name. iPXE's own `initrd` command
reference states: *"initrd [--name <name>] [--timeout <timeout>] <uri>
[<arguments>...]"*, and *"Any argument supplied to the initrd command will
be used as the pathname for that image within the … initial RAM filesystem"*
([ipxe.org/cmd/initrd](https://ipxe.org/cmd/initrd)). In BIOS mode, iPXE
synthesizes a CPIO header around the raw fetched file so the Linux kernel's
initramfs unpacker accepts it as one more file in the concatenated cpio
stream, alongside the "real" `initrd.img`. The practical effect: the entire
downloaded ISO ends up sitting in RAM at `/proxmox.iso` inside the booted
initramfs, before the installer's own init script ever runs.

This is confirmed independently by `pve-installer`'s own boot script,
`unconfigured.sh` (read directly from
[github.com/proxmox/pve-installer](https://raw.githubusercontent.com/proxmox/pve-installer/master/unconfigured.sh)),
which checks for the auto-install marker at a fixed, already-mounted path:

```bash
if [ -f /cdrom/auto-installer-mode.toml ]; then
    echo "Fetching answers for automatic installation"
    ...
```

`/cdrom` is presumably where something (not sourced in this research — see
§8) loop-mounts the `/proxmox.iso` file the initrd stage deposited. The exact
mechanism that performs that loop-mount (an initramfs script, a udev rule,
something else) was **not found in this research** and is listed as an open
question in §8 — but the *existence* of the `/proxmox.iso`-as-a-file
convention, and the fact that `unconfigured.sh` looks for its mounted
contents at a fixed path, are both directly sourced.

One documentation nuance worth flagging honestly: iPXE's own `initrd` page
notes *"For older kernels (before Linux 5.7), you will need to add the
kernel command-line argument `initrd=initrd.magic` when booting in UEFI
mode"* — implying the raw-named-file trick does work under UEFI on
sufficiently recent kernels (Proxmox VE 9 ships kernel 6.14.8, per public
release notes, well past that threshold) — while an iPXE mailing-list thread
elsewhere describes UEFI's kernel EFI stub as having no support for raw
(non-cpio) files at all. **These two sources are in tension and this
research did not resolve it.** It does not affect this project's
recommendation either way: this hardware's iUSB boot chain is legacy
BIOS (`docs/hardware-evidence-2026-08-08.md`'s El Torito findings), so only
the BIOS-mode behavior — which is unambiguous — matters here.

### The `--fetch-from` enum, precisely

Read directly from `proxmox-auto-install-assistant/src/main.rs`:

```rust
pub enum FetchAnswerFrom {
    Iso,
    Http,
    Partition,
}
```

**`pxe` is not a `--fetch-from` value.** It is a separate boolean flag
(`--pxe`) plus an optional `--pxe-loader` value. `docs/proxmox-autoinstall.md`'s
"command reference" section describes the tool as supporting *"`--fetch-from
http|partition|pxe`"* — based on this research's direct reading of current
upstream source, that phrasing conflates the two independent options; the
accurate statement is `--fetch-from {iso,http,partition}` controls **only**
where the *answer file* comes from, and `--pxe`/`--pxe-loader` is an
orthogonal flag controlling whether netboot-shaped output files are produced
at all. This is a minor correction to an existing project document, not a
new finding that changes any recommendation — noted here for consistency
across `docs/`.

---

## 4. Producing the iPXE bootstrap without a compiler toolchain

### Why the obvious method needs a compiler

iPXE's documented way to bake a script into a binary is the `EMBED=` build
parameter: *"`make bin/undionly.kpxe EMBED=myscript.ipxe`"*
([ipxe.org/embed](https://ipxe.org/embed)) — this is a full source build.
"To change the embedded script, you would need to rebuild the iPXE binary."
Running a C build toolchain at Ansible run time is exactly the kind of
standing infrastructure/tooling dependency this project has generally
avoided (see the container-fallback pattern in `asmb8_autoinstall_iso`,
which only reaches for a container when a *vendor-shipped* tool is absent —
never a from-source compile).

### The documented, no-rebuild alternative

The same `ipxe.org/embed` page documents an alternative explicitly designed
to avoid this:

> "Pass in an initrd to iPXE, for example by specifying the script file in a
> GRUB configuration when loading iPXE… The script file is a plain iPXE
> script file; there is no need to use a tool such as `mkinitrd`… You can
> change the embedded script by editing the script file, with no need to
> rebuild the iPXE binary."

with the worked GRUB example:

```
kernel (hd0,0)/ipxe.lkrn
initrd myscript.ipxe
```

`ipxe.lkrn` is a prebuilt iPXE image in Linux `bzImage` format, deliberately
shaped so ordinary Linux bootloaders (GRUB, LILO, syslinux) load it exactly
as if it were a Linux kernel and hand it a command line and initrd the same
way. **This means the entire embedding step is just "write a text file" —
no compiler, no `make`, no `EMBED=` rebuild.** GRUB2's modern equivalent of
the legacy `kernel`/`initrd` pair is `linux`/`initrd`; this research did not
find a GRUB2-specific worked example on an official page (the quoted example
above is legacy-syntax), but the underlying mechanism (treat `ipxe.lkrn` as
a Linux kernel, hand it a script as its initrd) is documented at the source
above and is architecturally the same regardless of which GRUB config
syntax loads it — **treat the exact GRUB2 command spelling as something to
confirm, not as verbatim-sourced.**

### What ships this project would need, and their actual sizes

Measured directly against `boot.ipxe.org`'s public downloads via HTTP `HEAD`
(2026-08-09, a request to a public internet mirror, not to any BMC or lab
host):

| File | Size (measured) | Purpose |
|---|---|---|
| `ipxe.lkrn` | 391,065 bytes (≈ 382 KiB) | The prebuilt "Linux-format" iPXE binary GRUB loads as if it were a kernel |
| `undionly.kpxe` | 72,272 bytes (≈ 70.6 KiB) | A PXE-ROM-hosted iPXE variant — not directly relevant here, listed for scale |
| `ipxe.iso` | 9,437,184 bytes (≈ 9.0 MiB) | Prebuilt, directly bootable ISO with **no embedded script** — useful only as a size reference, see below |
| `ipxe.usb` | 8,388,608 bytes (≈ 8.0 MiB) | Same, as a USB disk image |

**Why the prebuilt `ipxe.iso`/`ipxe.usb` alone are not sufficient on their
own**, despite comfortably fitting the 16 MiB budget: `ipxe.org/download`
itself says *"to use iPXE fully, you will need to build an appropriate image
from source"* — the prebuilt images carry no embedded script and, absent
one, iPXE falls back to its interactive shell prompt. This project's own KVM
channel has "zero live-hardware evidence and zero unit/mock test coverage"
of the handshake needed to inject keystrokes (`docs/roadmap.md`, describing
`asmb8_console`) — there is no way to drive an interactive iPXE shell
non-interactively today. The GRUB+`ipxe.lkrn`+script-file approach above is
recommended specifically because it produces **non-interactive** behavior
without a rebuild.

**Realistic total size**: `ipxe.lkrn` (382 KiB) plus a script file (well
under 1 KiB — see §5 for its contents) plus whatever `grub-mkrescue`'s own
El Torito/core-image overhead adds. `grub-mkrescue` is a standard tool
shipped in Debian/Ubuntu's `grub-pc-bin`/`grub-efi-amd64-bin`/`grub-common`
packages — the same category of already-available, no-compile tooling this
project already depends on for `xorriso` (`docs/proxmox-autoinstall.md`'s
container-fallback path). **This research did not actually run
`grub-mkrescue` and measure the resulting image** — general community
guidance found during this research describes grub-mkrescue's *default*
output (which bundles every available module, font, and theme) as commonly
several MiB, trimmable with `--install-modules=<list>` to include only the
handful of modules actually needed (biosdisk, iso9660, normal, linux16,
part_msdos — no filesystem driver beyond ISO9660 itself is required). No
authoritative single number was found. **Treat "the final bootstrap ISO fits
comfortably under 16 MiB" as a well-supported expectation, not a measured
fact** — see §8.

### Explicitly disqualified: anything that leans on DHCP or PXE infrastructure

Per the brief, this project has already rejected standing PXE/DHCP/TFTP
infrastructure — that is `asmb8_baremetal_install`'s entire reason to exist
(its README's opening line: *"no PXE/DHCP/TFTP/NFS/CIFS infrastructure
required"*). Several netboot-adjacent options were considered and are
disqualified on that basis alone, regardless of how well they otherwise
work:

- **Using the prebuilt images' default behavior** (chaining to
  `boot.ipxe.org`'s own public menu, or a script embedded by a third party)
  — these assume DHCP-obtained connectivity and a route to the public
  internet, neither guaranteed nor wanted here.
- **DHCP option 66/67 (`next-server`/`filename`) or a `pxe-loader` config
  file staged on a TFTP server** — this is exactly the "someone already runs
  a DHCP/TFTP server" assumption the whole design exists to avoid. Proxmox's
  own generated `boot.ipxe` assumes this by opening with a bare `dhcp` call
  (see §5) — that call must be replaced, not relied on.
- **Answer-file discovery via DHCP options 250/251**, which Proxmox's own
  `unconfigured.sh` explicitly wires up when `proxauto=1`/`proxmox-start-
  auto-installer` is set (quoted in full in §5) — not used here because this
  design uses `--fetch-from iso` specifically to avoid needing the answer
  file fetched at all at boot time.

---

## 5. Static IP: exact syntax, and where DHCP still lurks

### Setting a static address in an iPXE script

Community-sourced examples (no single official `ipxe.org` page dedicated to
static IP configuration was found — `ipxe.org/howto/dhcp` does not exist as
of this research) converge on the same pattern. From a maintained gist of
static-vs-DHCP iPXE examples
([gist.github.com/tuxfight3r](https://gist.github.com/tuxfight3r/76827b554443260c9410b5196bbc5148))
and corroborating community posts:

```
set net0/ip 192.168.1.240
set net0/netmask 255.255.255.0
set net0/gateway 192.168.1.1
set net0/dns 192.168.1.1
ifopen net0
```

(`set dns <addr>` — a global setting rather than a per-interface one — also
appears in some examples in place of `net0/dns`; both forms were found in
circulation. This research did not find a single authoritative page
resolving which is canonical, and this is flagged as an open question in
§8.) This is precisely the pattern the brief mentions a sibling homelab
design already needed, for the same reason: DHCP is not reliably available
on this network.

**Do not put a bare `dhcp` command in the bootstrap script.** iPXE's own
scripting documentation states plainly: *"iPXE will terminate a script
immediately if any line of the script fails"* and that the `dhcp` command is
subject to the same rule — it can only be made non-fatal with `dhcp || …`
([ipxe.org/scripting](https://ipxe.org/scripting)). Proxmox's generated
`boot.ipxe` (§3) opens with an unconditional `dhcp` for exactly this reason —
it assumes DHCP is present and wants to fail loudly if it is not. For this
project's environment, the fix is simply to **omit `dhcp` entirely** and use
the static block above instead; there is no need for a fallback branch if
static configuration is the only path this design supports.

The resulting bootstrap script (patching only Proxmox's own generated
`boot.ipxe` template from §3) looks like:

```
#!ipxe

set net0/ip 192.168.1.50
set net0/netmask 255.255.255.0
set net0/gateway 192.168.1.1
set net0/dns 192.168.1.1
ifopen net0

kernel http://192.168.1.10/proxmox-auto/vmlinuz ramdisk_size=16777216 rw quiet console=ttyS0,115200 splash=silent proxmox-start-auto-installer
initrd http://192.168.1.10/proxmox-auto/initrd.img
initrd http://192.168.1.10/proxmox-auto/proxmox-ve_9.2-1.iso proxmox.iso
boot
```

(Absolute URLs are used here for clarity; relative filenames, resolved
against the URL this script itself was fetched from, work identically per
§3 and would let the same script move between HTTP servers unmodified.)

### Where DHCP still lurks, even in this design

Read directly from `pve-installer`'s `unconfigured.sh` (the initrd's own
PID-1 replacement, fetched in full from
[github.com/proxmox/pve-installer](https://raw.githubusercontent.com/proxmox/pve-installer/master/unconfigured.sh)):

```bash
# try to get ip config with dhcp
echo -n "Attempting to get DHCP leases... "
dhclient -v
echo "done"
```

This call is **unconditional** — it runs regardless of `--fetch-from`
mode, `proxauto`, or any other flag. There is **no `ip=`-style kernel
parameter, and no other sourced mechanism, for telling the live installer
environment itself to use a static address instead of DHCP** — this
research read the entire script and found no such branch. The only
DHCP-related conditional gates a *second* concern: whether
`/etc/dhcp/dhclient.conf` should also request the Proxmox-specific
options 250/251 for answer-file discovery (`if [ $start_auto_installer -ne 0
]`), which is irrelevant to this design because it uses `--fetch-from iso`.

**Practical consequence for this design**: using `--fetch-from iso` (as
recommended in §1) means the *live installer environment's* `dhclient -v`
call becomes a no-op with respect to whether the install can proceed — the
answer file and the installer image are already sitting in RAM by the time
`unconfigured.sh` runs, fetched entirely by iPXE before `boot` was ever
called. Whether `dhclient -v` finding no DHCP server **delays** boot (by
however long its configured timeout is — a Proxmox forum thread reports it
hardcoded to roughly 10 seconds inside the installer's own `dhclient.conf`,
[forum.proxmox.com](https://forum.proxmox.com/threads/dhcp-race-with-interface-configuration-during-automated-installation.150701/))
or **blocks** it (the script has a global `trap "err_reboot" ERR`; whether
`dhclient`'s own exit code under a completely absent DHCP server trips that
trap was not confirmed one way or the other in this research — ISC
`dhclient`'s typical behavior is to background itself and keep retrying
rather than exit non-zero, which would make this a non-issue, but this was
not independently verified against the specific `dhclient` build Proxmox
ships) is listed as an open question in §8. **This is exactly the kind of
detail that must be watched for on the first real hardware attempt** — a
several-second delay at this point during a real install is expected and
harmless; an installation aborting outright at this exact point would be the
signal that this assumption was wrong.

---

## 6. Alternatives considered

Ranked by how well each fits "Ansible-driven, zero standing infrastructure,
no compiler":

### 1. (Recommended) iPXE via prebuilt `ipxe.lkrn` + GRUB + static script — §1–§5 above
No compiler, no DHCP, reuses Proxmox's own official, version-matched netboot
mechanism unmodified except for the network-configuration lines. The
weakest link is the unmeasured final ISO size (§4) and the unresolved
`dhclient` behavior (§5) — both cheap to close with one real build/boot,
neither large enough to change the recommendation.

### 2. GRUB's own native HTTP netboot support, skipping iPXE entirely
GRUB2 has an `http` module and network commands (`net_bootp`,
`net_add_addr`, `net_default_server`) documented in the GNU GRUB manual's
networking chapter, and can load a kernel directly from an `(http,server)/…`
device path — search results describing a working `net_default_server=(http,
10.0.0.1)` / `root=http,10.0.0.1` example were found, though this research
could not reliably re-fetch the GNU GRUB manual's own networking pages
directly (repeated HTTP 429 responses from `gnu.org` during this research)
to confirm the exact static-IP command syntax end-to-end. A real,
independently-relevant risk: a **2022 Debian bug report**
([groups.google.com mirror of Debian bug #1016873](https://groups.google.com/g/linux.debian.bugs.dist/c/CqfwbhAd-Xg))
found that Debian's *netboot-specific* GRUB images did not include the
`http` module by default and required regenerating the boot image with a
different module list to add it — not a source rebuild, but not a
"just works out of the box" story either. This is a viable second choice,
structurally similar in spirit to the recommendation above (no compiler,
just packaging), but this research came away less confident in the exact
command sequence than it did for the iPXE path, where Proxmox's own shipped
tooling supplies a verified-correct template to start from. **Rank below
option 1 specifically because option 1 has a vendor-supplied, version-
matched reference script to adapt; this option would require deriving the
whole script from manual pages this research could not fully verify.**

### 3. `proxmox-auto-install-assistant --fetch-from http` alone (answer file only), still attaching the full ISO over iUSB
This solves a real but different problem — letting the answer file be
changed without rebuilding the ISO — and does nothing for the bulk-transfer
problem this document exists to solve. `docs/proxmox-autoinstall.md`
correctly scopes this as orthogonal; not a netboot alternative on its own.

### 4. `memdisk` (syslinux)
This is the older, BIOS-only tool that inspired the community's own
`sanboot` workaround discussed in §2 — load the whole ISO into RAM, then
have `memdisk` present it as an INT13 drive. Proxmox's own multi-`initrd`
mechanism (§3) already achieves the same "whole ISO in RAM" result more
directly, without a second BIOS-emulation layer and without `memdisk`'s
BIOS-only restriction. No reason to reach for this when Proxmox's own
tooling already does the equivalent, better.

### 5. A small Linux image that fetches the real installer and `kexec`s into it
Strictly more moving parts than option 1 for no clear benefit here: it would
still need a compiler-free way to build that small Linux image (the same
class of problem §4 solves for iPXE), and Proxmox's own multi-`initrd`
mechanism already gets the entire installer image into RAM in one step
without a `kexec` handoff at all. Not pursued further.

---

## 7. Documented vs. inferred — summary

| Claim | Status |
|---|---|
| iPXE `sanboot` does not survive a Linux kernel switch for a device with no independent hardware transport | **Documented**, primary iPXE-maintainer source (§2) |
| Proxmox's own `--pxe` mechanism never uses `sanboot`; it loads the whole ISO as a raw `initrd` segment | **Documented**, read directly from `proxmox-auto-install-assistant` source (§3) |
| `--pxe`/`--pxe-loader ipxe` exist, and their exact CLI surface (`--fetch-from` values, validation rules) | **Documented**, read directly from upstream source (§3) |
| The generated `boot.ipxe` template's literal text | **Documented**, quoted verbatim from source (§3) |
| The exact final kernel command line the tool emits for the automated-install entry (fragment concatenation order, whether `vga=788` is included) | **Inferred** — assembled from sourced fragments, not captured as a literal tool output (§3) |
| The `initrd <uri> <name>` raw-file-injection mechanism and its BIOS-mode behavior | **Documented**, `ipxe.org/cmd/initrd` (§3) |
| Whether that mechanism works identically under UEFI on modern kernels | **Conflicting sources, unresolved** (§3) — irrelevant to this hardware (legacy BIOS) either way |
| What loop-mounts `/proxmox.iso` onto `/cdrom` before `unconfigured.sh` checks it | **Not sourced at all** — open question (§3, §8) |
| `unconfigured.sh` calls `dhclient -v` unconditionally, with no static-IP kernel parameter anywhere in the script | **Documented**, full script read directly (§5) |
| Whether an absent DHCP server causes `dhclient -v` to block/fail installation, or merely delay it harmlessly | **Unverified** — plausible inference (backgrounding is typical ISC `dhclient` behavior) but not independently confirmed for Proxmox's exact build (§5, §8) |
| iPXE static-IP script syntax (`set net0/ip` etc.) | **Community-sourced, converging on one pattern, but no single official ipxe.org page found** (§5) |
| Prebuilt `ipxe.lkrn`/`ipxe.iso`/`ipxe.usb`/`undionly.kpxe` sizes | **Measured directly** against a public download, 2026-08-09 (§4) |
| The final bootstrap ISO (`ipxe.lkrn` + script, wrapped by `grub-mkrescue`) fits the 16 MiB budget | **Well-supported expectation, not measured** — `grub-mkrescue` was never actually run in this research (§4, §8) |
| GRUB2's exact static-IP-over-HTTP command sequence (option 2 in §6) | **Not independently confirmed end-to-end** in this research (§6) |

---

## 8. Open questions / must verify on hardware

Listed honestly as "not yet known," matching this collection's own standard
(`docs/roadmap.md` §5):

1. **Actually build and measure the bootstrap ISO.** Run `grub-mkrescue`
   with `ipxe.lkrn` and a real static-IP script, confirm it boots at all
   under BIOS/legacy mode (matching this hardware's proven El Torito boot
   chain — `docs/hardware-evidence-2026-08-08.md`), and record its real
   size against the 16 MiB budget. Nothing in this document should be read
   as "this has been built," only "this should build."
2. **Confirm what loop-mounts the injected `/proxmox.iso` file to `/cdrom`.**
   This research found the convention (`unconfigured.sh` checking
   `/cdrom/auto-installer-mode.toml`) but not the mechanism that puts it
   there for a netboot (as opposed to a physically-attached CD) boot. If
   this step turns out to depend on something PXE/TFTP-specific rather than
   being generic to "any file named `proxmox.iso` appears in the
   initramfs," that would materially affect this design and needs to be
   caught before relying on it.
3. **Confirm `dhclient -v`'s real behavior with zero DHCP servers present**
   on this specific network — does it block/abort (tripping
   `unconfigured.sh`'s `ERR` trap) or background harmlessly after its
   ~10-second timeout? This determines whether `--fetch-from iso` alone is
   sufficient insulation from the "no DHCP on this LAN" problem, or whether
   a DHCP server needs to exist on this specific network segment purely to
   keep this one call quiet (which would not reintroduce the original
   PXE-boot-server dependency, but would be a real, previously-unstated
   requirement worth documenting explicitly if confirmed).
4. **Confirm the exact GRUB2 command syntax** for loading `ipxe.lkrn` plus a
   script-as-initrd (§4) — the sourced example uses legacy GRUB syntax;
   translate and verify against whatever GRUB2 version `grub-mkrescue`
   actually produces.
5. **Confirm `net0/dns` vs. the global `dns` setting** for static DNS
   configuration in iPXE (§5) — both were found in circulation with no
   single authoritative resolution in this research. Low-stakes (DNS is not
   needed anywhere in this specific boot chain, since every fetch target is
   a hardcoded IP address, not a hostname), but worth getting right if this
   script is ever extended.
6. **Confirm the tool actually emits the exact fetch-from/pxe combination
   assumed in §3** — this research read the CLI parser's validation logic,
   not an actual run of the tool. `asmb8_autoinstall_iso`'s existing
   `validate-answer` pattern (`docs/proxmox-autoinstall.md`) is the right
   model for building confidence here before trusting this in a real
   pipeline: run the real tool, inspect its real output directory, before
   writing any Ansible automation around it.
7. **UEFI vs. BIOS for the raw-`initrd`-file trick** (§3) — not expected to
   matter for this specific board (legacy BIOS boot chain, per
   `docs/hardware-evidence-2026-08-08.md`), but worth resolving properly if
   this design is ever pointed at different hardware.

---

## 9. What this document rules out, and why

- **`sanboot` of the HTTP-hosted Proxmox ISO** — real, sourced structural
  limitation (§2). Ruled out, not merely deprioritized.
- **Any reliance on standing DHCP/PXE/TFTP infrastructure** (default
  prebuilt-image behavior, DHCP options 66/67/250/251, `next-server`) — the
  brief's own constraint, and this project's existing
  `asmb8_baremetal_install` design principle. Ruled out per §4's explicit
  disqualification list, regardless of how well any of it otherwise works.
- **Compiling iPXE from source with `EMBED=`** — works, but reintroduces a
  build-toolchain dependency at Ansible runtime that the GRUB+`ipxe.lkrn`
  alternative avoids entirely for the same outcome (§4). Not ruled out on
  correctness grounds — ruled out on cost grounds, in favor of a strictly
  cheaper option that reaches the same place.
- **`memdisk`** — superseded by Proxmox's own multi-`initrd` mechanism,
  which achieves the identical "whole ISO resident in RAM" result without
  `memdisk`'s BIOS-only restriction or its extra emulation layer (§6).
- **Fetching only the answer file over HTTP while still attaching the full
  ISO over iUSB** — does not address the problem this document exists to
  solve; kept as a separate, already-implemented, orthogonal capability in
  `asmb8_autoinstall_iso` (§6).

## Sources consulted

- Proxmox VE Automated Installation wiki:
  <https://pve.proxmox.com/wiki/Automated_Installation>
- `proxmox-auto-install-assistant` source, `proxmox/pve-installer`
  (`main.rs`, `unconfigured.sh`, `debian/changelog`), read directly from
  <https://github.com/proxmox/pve-installer> at its `master` branch,
  2026-08-09.
- iPXE command reference: <https://ipxe.org/cmd/sanboot>,
  <https://ipxe.org/cmd/initrd>, <https://ipxe.org/embed>,
  <https://ipxe.org/scripting>, <https://ipxe.org/download>.
- iPXE community/maintainer discussion of `sanboot`'s kernel-handoff
  limitation: <https://github.com/ipxe/ipxe/discussions/912>;
  <https://lists.ipxe.org/pipermail/ipxe-devel/2017-January/005436.html>.
- Static-IP iPXE script examples:
  <https://gist.github.com/tuxfight3r/76827b554443260c9410b5196bbc5148> and
  corroborating community search results (no single canonical `ipxe.org`
  page found).
- GNU GRUB manual (networking chapter; partially inaccessible during this
  research due to rate-limiting — see §6, §8) and Debian bug #1016873 on the
  `http` module's absence from default netboot images:
  <https://groups.google.com/g/linux.debian.bugs.dist/c/CqfwbhAd-Xg>.
- Proxmox forum, on `dhclient`'s ~10-second timeout inside the installer
  environment:
  <https://forum.proxmox.com/threads/dhcp-race-with-interface-configuration-during-automated-installation.150701/>.
- Direct HTTP `HEAD` measurements against `boot.ipxe.org`'s public
  downloads, 2026-08-09 (a public internet request, not to any BMC or lab
  host).
- This collection's own `docs/hardware-evidence-2026-08-08.md` and
  `docs/proxmox-autoinstall.md`, for the measured iUSB throughput figures,
  the `auto-installer-mode.toml` GRUB gate, and the existing
  `asmb8_autoinstall_iso`/`asmb8_baremetal_install` architecture this design
  extends.
