<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_bootstrap_image`

Build a size-budgeted, bootable iPXE bootstrap image that chains to an HTTP
origin. Runs entirely on the Ansible controller — it never contacts a BMC.

## Synopsis

Builds a small, bootable ISO carrying a prebuilt iPXE binary and an embedded
script that brings up the target's real NIC and `chain`s to an HTTP origin —
typically an ephemeral session started by
[`asmb8_http_origin`](asmb8_http_origin.md). Attaching this image over iUSB
instead of a full installer ISO is this collection's fix for
[`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md)'s
measured ~790-900 KB/s iUSB throughput: the bulk installer transfer moves
over plain HTTP at LAN speed instead, entirely bypassing the BMC. See
[`docs/netboot-design.md`](netboot-design.md) for the research this module
implements — read that document first; this module follows its
recommendation rather than re-deriving it.

## Why a prebuilt `ipxe.lkrn` plus `grub-mkrescue`, not a source build or a container

`docs/netboot-design.md` section 4 rules out iPXE's own `EMBED=` build
parameter specifically because it needs a full C toolchain present at
run time — exactly the standing-infrastructure dependency this collection
otherwise avoids. This module instead wraps a prebuilt `ipxe.lkrn` (a static
~382 KiB binary shaped so GRUB loads it exactly like a Linux kernel) with a
GRUB2 configuration built by `grub-mkrescue`, passing the embedded script as
that "kernel"'s initrd rather than compiling it in — iPXE's own documented,
no-rebuild alternative to `EMBED=`.

**The trade-off, stated honestly.** A Docker-based
`make bin/ipxe.iso EMBED=...` path (also described in
`docs/netboot-design.md`) needs no local GRUB/`xorriso` install, but assumes
a container runtime is present. A crash-cart laptop carried to a site with no
other infrastructure may well not have Docker; `grub-mkrescue`/`xorriso` are
ordinary Debian/Ubuntu packages (`grub-pc-bin`/`grub-common`, `xorriso`) with
no daemon and no image pull required to invoke — the better fit for this
collection's "no standing infrastructure" principle, at the cost of requiring
those two tools already installed rather than reaching for a self-contained
fallback the way `asmb8_autoinstall_iso` does for its own `xorriso` rebuild
path.

**This module never fetches `ipxe.lkrn` itself.** `ipxe_lkrn_path` must
already exist on the controller — caching that one small, static file is the
caller's job, so this module makes no network request of any kind.

## What remains unverified

Stated as plainly as `docs/netboot-design.md` states its own open questions
(see that document's section 8):

- **The exact GRUB2 command syntax.** That document found only a legacy-GRUB
  (`kernel`/`initrd`) worked example and explicitly flagged the GRUB2
  translation as unconfirmed. This module renders `linux16`/`initrd16` (the
  16-bit real-mode Linux boot protocol) rather than plain `linux`/`initrd` —
  the same choice real-world GRUB2 configurations make for `memtest86+`,
  another non-Linux payload reusing the Linux kernel image format the way
  `ipxe.lkrn` does. This is this module's own best-effort translation, not
  something `docs/netboot-design.md` verified, and it has never been run
  against a real `grub-mkrescue` or booted on real hardware.
- **The final image's real size.** `docs/netboot-design.md` section 4 never
  actually ran `grub-mkrescue` to measure a real result — "fits under 16
  MiB" is called a well-supported expectation there, not a measured fact.
  This module's size-budget enforcement is what turns that expectation into
  "provably small, or refused," without trusting the estimate.
- **The GRUB2-source-directory merge convention.** `grub-mkrescue <dir>`
  overlaying `<dir>/boot/grub/grub.cfg` into the ISO it produces is a widely
  used community recipe, not something either this module or
  `docs/netboot-design.md` independently confirmed against this exact
  `grub-mkrescue` version.

See [`docs/capability-matrix.md`](capability-matrix.md) for where this
module's own claims land in that document's confidence tiers.

## Options

| Option | Type | Default | Required | Choices |
|---|---|---|---|---|
| `origin_url` | `str` | — | yes | — |
| `ipxe_lkrn_path` | `path` | — | yes | — |
| `output_path` | `path` | — | yes | — |
| `network_mode` | `str` | `static` | no | `static`, `dhcp` |
| `address` | `str` | — | when `network_mode=static` | — |
| `netmask` | `str` | — | when `network_mode=static` | — |
| `gateway` | `str` | — | when `network_mode=static` | — |
| `dns` | `str` | — | no | — |
| `size_budget_bytes` | `int` | `16777216` (16 MiB) | no | — |
| `grub_mkrescue_path` | `path` | — | no | — |
| `xorriso_path` | `path` | — | no | — |
| `work_dir` | `path` | — | no | — |

Verified against `argument_spec()` in `plugins/modules/asmb8_bootstrap_image.py`.

### `network_mode`

`static` (the default) requires `address`/`netmask`/`gateway` and renders
`set net0/ip ...`/`ifopen net0` lines, never a bare `dhcp` command.
`docs/netboot-design.md` section 5 is explicit that iPXE aborts a script on
any failing line and recommends never emitting a bare `dhcp` for exactly
that reason; the brief this module was built against records an earlier
attempt that failed to obtain a DHCP lease on this project's own network.
`dhcp` renders a bare `dhcp` command instead — an explicit caller opt-out for
a network known to have working DHCP, never this module's own
recommendation.

### `size_budget_bytes`

Hard cap, in bytes, on the built image's size — see the module's own
description. `docs/netboot-design.md`'s hand-built reference image measured
0.89 MiB; the sibling homelab design mentioned in this module's own brief
uses a 16 MiB limit, which is this option's default.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `output_path` | `str` | always | Same value as `output_path`. |
| `size_bytes` | `int` | when available | The built image's actual size. `null` in check mode, since nothing was actually built. |
| `size_budget_bytes` | `int` | always | The effective budget enforced for this build. |
| `script` | `str` | always | The embedded iPXE script this build rendered, for diagnosis. |
| `grub_mkrescue_path` | `str` | when available | The resolved `grub-mkrescue` path actually used. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"asmb8_bootstrap_image.build"`. |
| `operation.endpoint` | `str` | always | Same value as `output_path` — this module never contacts a BMC, so there is no BMC endpoint to report. |
| `operation.changed` | `bool` | always | This module has no idempotent no-op case; always `true` on success. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_bootstrap_image.py`.

## `error_class` values this module can raise

- `unsupported_capability` — `grub-mkrescue` or `xorriso` could not be found,
  either on `PATH` or at an explicit `grub_mkrescue_path`/`xorriso_path`. The
  failure message names the Debian/Ubuntu packages to install. This module
  never falls back to a container runtime or a source build.
- `protocol` — `ipxe_lkrn_path` does not exist, `grub-mkrescue` exited
  non-zero, `grub-mkrescue` reported success but produced no output file, or
  the built image exceeded `size_budget_bytes` (the oversized file is
  deleted before this is raised).

## Check-mode behaviour

Full support. Validates every option, confirms `ipxe_lkrn_path` exists, and
confirms `grub-mkrescue`/`xorriso` can be found — but never invokes
`grub-mkrescue` and never writes `output_path`. Because nothing is actually
built, check mode cannot confirm the result would fit `size_budget_bytes`;
`size_bytes` is `null` in check mode for exactly this reason. `diff_mode` is
not supported.

## Example

```yaml
- name: Build a static-IP bootstrap image chaining to an already-running HTTP origin
  james_crowley.asmb8_ikvm.asmb8_bootstrap_image:
    origin_url: "{{ origin.url }}boot.ipxe"
    ipxe_lkrn_path: /srv/netboot/ipxe.lkrn
    output_path: /srv/netboot/bootstrap.iso
    network_mode: static
    address: 192.0.2.50
    netmask: 255.255.255.0
    gateway: 192.0.2.1
  delegate_to: localhost
  register: bootstrap

- name: Attach the built bootstrap image instead of the full installer ISO
  james_crowley.asmb8_ikvm.asmb8_media:
    image: "{{ bootstrap.output_path }}"
    state: attached
  delegate_to: localhost
```

## See also

- [`asmb8_http_origin`](asmb8_http_origin.md) — the ephemeral HTTP origin
  this module's built image is meant to `chain` to.
- [`asmb8_media`](asmb8_media.md) — attaches the built image over iUSB.
- [`docs/netboot-design.md`](netboot-design.md) — the research behind this
  module's every design choice.
- The `asmb8_baremetal_install` role's `ipxe_http` delivery mode, which wires
  this module, `asmb8_http_origin`, and `asmb8_media` together end to end.
