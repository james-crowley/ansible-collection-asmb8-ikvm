# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Build a size-budgeted, bootable iPXE bootstrap image for ``asmb8_bootstrap_image``.

This exists to close the gap ``docs/netboot-design.md`` describes but does not
implement: that document measured this BMC's iUSB virtual-CD channel at
~790-900 KB/s and worked out, from public documentation and source code alone
(no BMC contacted), that the fix is to attach only a **small bootstrap image**
over iUSB whose one job is to bring up the target's real NIC and hand off to
an HTTP origin at LAN speed -- see that document's section 1 for the design
in full, and section 4 for why this file builds that image the way it does.

**Why a prebuilt ``ipxe.lkrn`` plus ``grub-mkrescue``, not a source build or a
container.** ``docs/netboot-design.md`` section 4 works out that iPXE's own
documented ``EMBED=`` build parameter needs a full C toolchain at run time --
exactly the standing-infrastructure dependency this collection has otherwise
avoided (see that document's own citation of ``asmb8_autoinstall_iso``'s
container-fallback pattern, which only reaches for a container when a
*vendor-shipped* tool is absent, never a from-source compile). The same
document's own alternative -- a prebuilt ``ipxe.lkrn`` (measured directly
against a public download, 391,065 bytes) handed to GRUB as if it were a
Linux kernel, with the embedded script passed as its *initrd* rather than
compiled in -- needs no compiler at all, just a text file and a tool
(``grub-mkrescue``) already in this collection's existing tool category
(alongside ``xorriso``, which ``asmb8_autoinstall_iso``'s container fallback
already depends on). This module follows that recommendation. The trade-off,
stated honestly: a Docker-based ``make bin/ipxe.iso EMBED=...`` path (also
described in that document) would also work and needs no local GRUB/xorriso
install, but assumes a container runtime is present -- which a crash-cart
laptop carried to a site with no other infrastructure may well not have.
``grub-mkrescue``/``xorriso`` are ordinary Debian/Ubuntu packages
(``grub-pc-bin``/``grub-common``, ``xorriso``) with no daemon, no image pull,
and no network access required to invoke -- the better fit for this
collection's "no standing infrastructure" principle, even though it means
this module (unlike the Docker path) requires those two tools to already be
installed rather than reaching for a self-contained fallback the way
``asmb8_autoinstall_iso`` does. See this module's own ``DOCUMENTATION`` for
the same trade-off restated for an operator, not a maintainer.

**What remains UNVERIFIED, stated as plainly as ``docs/netboot-design.md``
itself states its own open questions (see that document's section 8):**

* The exact GRUB2 command syntax for loading ``ipxe.lkrn`` plus a script as
  its initrd. ``docs/netboot-design.md`` section 4 found only a *legacy*
  GRUB (``kernel``/``initrd``) worked example on ``ipxe.org`` and explicitly
  flagged the GRUB2 translation as unconfirmed. This module uses GRUB2's
  ``linux16``/``initrd16`` commands (the 16-bit real-mode Linux boot
  protocol) rather than plain ``linux``/``initrd`` (GRUB2's newer 32/64-bit
  protocol path) -- the same choice real-world GRUB2 configurations make for
  ``memtest86+``, another non-Linux payload that reuses the Linux kernel
  image format the same way ``ipxe.lkrn`` does, for the same reason: the
  32-bit protocol handler assumes post-decompression behaviour a real Linux
  kernel implements and a payload like this does not. This is this module's
  own best-effort translation, not something ``docs/netboot-design.md``
  verified, and it has never been run against ``grub-mkrescue`` or booted on
  real hardware.
* The final bootstrap image's real size. ``docs/netboot-design.md`` section 4
  explicitly did not run ``grub-mkrescue`` to measure it, calling "fits
  under 16 MiB" a well-supported expectation, not a measured fact. This
  module's own size-budget enforcement (see :func:`build_bootstrap_image`)
  exists precisely because that expectation could be wrong -- the budget is
  what turns "probably small" into "provably small, or refused."

**Static IP versus DHCP.** ``docs/netboot-design.md`` section 5 is explicit
that a bare ``dhcp`` command must never appear in this design's own script --
iPXE aborts a script on any failing line, DHCP is not reliably available on
this project's network (the brief's own account of an earlier failed lease
attempt is the reason this module exists at all), and the sibling homelab
design mentioned in that same brief bakes in a static tuple for exactly that
reason. :data:`NETWORK_MODE_STATIC` is this module's default for that reason.
:data:`NETWORK_MODE_DHCP` is still offered -- for a network where DHCP is
known to work, forcing static configuration anyway would just be a second,
unnecessary way to get the address wrong -- but it is the caller's explicit
opt-out, not the default.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ProtocolError, UnsupportedCapabilityError, redact

#: iPXE's own scripting documentation (ipxe.org/scripting, cited in
#: docs/netboot-design.md section 5) states plainly that a script aborts on
#: any failing line -- ``static`` never emits a bare ``dhcp`` for exactly
#: that reason. See this module's own docstring above for the full reasoning.
NETWORK_MODE_STATIC = "static"
NETWORK_MODE_DHCP = "dhcp"
NETWORK_MODES = (NETWORK_MODE_STATIC, NETWORK_MODE_DHCP)

#: The sibling homelab design's own budget, per the brief this module was
#: built against; docs/netboot-design.md's own hand-built image measured
#: 0.89 MiB, comfortably inside it. Enforced by build_bootstrap_image() --
#: see that function's own docstring for why this exists at all.
DEFAULT_SIZE_BUDGET_BYTES = 16 * 1024 * 1024

#: External tools this module shells out to (grub-mkrescue directly; xorriso
#: is grub-mkrescue's own internal dependency, checked separately anyway so a
#: missing xorriso is diagnosed by name rather than surfacing only once
#: grub-mkrescue itself fails in a way that names it). Order matters only for
#: message composition, not for behaviour.
REQUIRED_TOOLS = ("grub-mkrescue", "xorriso")

#: Bounded diagnostic excerpt from a failed grub-mkrescue invocation. Mirrors
#: errors.MAX_DIAGNOSTIC_BYTES's own reasoning: a full stdout/stderr capture
#: never belongs whole in a task result.
_MAX_TOOL_OUTPUT_EXCERPT = 4096

#: GRUB2's linux16/initrd16 commands, not linux/initrd -- see this module's
#: own docstring for why. {kernel_name}/{script_name} are the basenames this
#: module itself controls (see _stage_directory), never caller input, so an
#: f-string here carries no injection risk.
_GRUB_CFG_TEMPLATE = """\
set timeout=0
set default=0

menuentry "asmb8-bootstrap" {{
    linux16 /boot/{kernel_name}
    initrd16 /boot/{script_name}
}}
"""

_KERNEL_BASENAME = "ipxe.lkrn"
_SCRIPT_BASENAME = "script.ipxe"


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """The network half of the embedded iPXE script -- see :func:`render_ipxe_script`."""

    mode: str
    address: str | None = None
    netmask: str | None = None
    gateway: str | None = None
    dns: str | None = None


def render_ipxe_script(*, origin_url: str, network: NetworkConfig) -> str:
    """Render the embedded iPXE script: bring up ``net0``, then ``chain`` to ``origin_url``.

    Deliberately minimal and generic -- this module has no opinion about what
    ``origin_url`` actually serves (a Proxmox-specific ``boot.ipxe``, or
    anything else an ``asmb8_http_origin`` session is handing out); its only
    job, per this module's own description, is bringing up the NIC and
    handing off. ``chain`` is iPXE's own command for fetching and executing
    another script or image by URL -- see ipxe.org's command reference.
    """
    lines = ["#!ipxe", ""]
    if network.mode == NETWORK_MODE_STATIC:
        lines.append(f"set net0/ip {network.address}")
        lines.append(f"set net0/netmask {network.netmask}")
        lines.append(f"set net0/gateway {network.gateway}")
        if network.dns:
            lines.append(f"set net0/dns {network.dns}")
        lines.append("ifopen net0")
    else:
        # docs/netboot-design.md section 5's own warning applies here too --
        # this branch is the caller's explicit opt-out, not this module's
        # recommendation. See this module's own docstring.
        lines.append("dhcp")
    lines.append("")
    lines.append(f"chain {origin_url}")
    lines.append("")
    return "\n".join(lines)


def find_tool(name: str, *, explicit_path: str | None = None) -> str | None:
    """Resolve ``name`` to an absolute, executable path, or ``None`` if it cannot be found.

    ``explicit_path``, when given, is trusted outright if it exists and is
    executable -- this is a caller override (e.g. a role variable pinning a
    non-PATH install), not something this function searches for itself.
    Otherwise falls back to :func:`shutil.which` against the current
    ``PATH``. Never raises -- callers decide what "not found" means (see
    :func:`build_bootstrap_image`, which turns a ``None`` into a classified,
    actionable failure rather than a traceback).
    """
    if explicit_path:
        candidate = Path(explicit_path)
        if candidate.is_file() and shutil.os.access(candidate, shutil.os.X_OK):  # type: ignore[attr-defined]
            return str(candidate)
        return None
    return shutil.which(name)


def missing_tool_error(tool_names: list[str]) -> UnsupportedCapabilityError:
    """Build the actionable failure for one or more missing external tools.

    Named separately from :func:`build_bootstrap_image` so the module layer
    can raise this before ever touching the filesystem (check_mode's own
    tool-presence check reuses this too) -- see this module's own docstring
    on why this collection refuses rather than lets a missing tool surface as
    a bare traceback.
    """
    joined = ", ".join(tool_names)
    return UnsupportedCapabilityError(
        f"asmb8_bootstrap_image requires the following external tool(s), not found on this controller's PATH: "
        f"{joined}. On Debian/Ubuntu, install grub-pc-bin (or grub-common, for a UEFI-only controller) and "
        "xorriso, e.g. `apt-get install grub-pc-bin xorriso`. This module never falls back to a container "
        "runtime or a source build -- see its own DOCUMENTATION for why a Docker-based alternative was "
        "considered and rejected as the default. Set grub_mkrescue_path/xorriso_path explicitly if either "
        "tool is installed somewhere not on PATH.",
        operation="asmb8_bootstrap_image.build",
    )


def _stage_directory(staging_root: Path, *, ipxe_lkrn_path: Path, script_text: str) -> Path:
    """Populate ``staging_root`` with the ``boot/grub/grub.cfg`` + ``boot/{kernel,script}`` tree
    ``grub-mkrescue`` merges into the ISO it builds. Returns ``staging_root`` for convenience.
    """
    boot_dir = staging_root / "boot"
    grub_dir = boot_dir / "grub"
    grub_dir.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(ipxe_lkrn_path, boot_dir / _KERNEL_BASENAME)
    (boot_dir / _SCRIPT_BASENAME).write_text(script_text, encoding="utf-8")
    (grub_dir / "grub.cfg").write_text(
        _GRUB_CFG_TEMPLATE.format(kernel_name=_KERNEL_BASENAME, script_name=_SCRIPT_BASENAME),
        encoding="utf-8",
    )
    return staging_root


def build_bootstrap_image(
    *,
    ipxe_lkrn_path: str,
    script_text: str,
    output_path: str,
    size_budget_bytes: int,
    grub_mkrescue_path: str,
    work_dir: str | None = None,
    run_command: Callable[..., Any],
) -> dict:
    """Build the bootstrap ISO at ``output_path`` and enforce ``size_budget_bytes``.

    ``run_command`` is REQUIRED and has deliberately no default. It must be a
    callable taking an argv list and returning an object with ``returncode``,
    ``stdout`` and ``stderr`` attributes.

    There is no default on purpose, and the reason is worth recording because a
    future tidy-up would otherwise reinstate one. Defaulting it to
    :func:`subprocess.run` reads as a convenience, but ``ansible-test``'s
    ``ansible-bad-function`` check rejects it: pylint infers the parameter's
    default at the call site and reports a raw ``subprocess.run`` call, because
    an Ansible module is expected to shell out through
    ``AnsibleModule.run_command`` -- which handles argument quoting, environment
    and error reporting consistently. That failure only appears on newer
    ansible-core (it was caught by CI on 2.21, having passed locally), so it is
    easy to reintroduce and slow to notice.

    Requiring the parameter also keeps the seam every unit test in this
    collection uses to avoid ever invoking a real ``grub-mkrescue``.

    Raises :class:`ProtocolError` for a missing/invalid ``ipxe_lkrn_path``, a
    non-zero ``grub-mkrescue`` exit, a missing output file despite a
    zero exit, or -- the property this whole module exists to guarantee --
    an output that exceeds ``size_budget_bytes``. In that last case the
    oversized file is deleted before raising: a "bootstrap" that silently
    grew past its budget must never be left in place looking like a valid
    result, per the brief this module was built against.
    """
    lkrn = Path(ipxe_lkrn_path)
    if not lkrn.is_file():
        raise ProtocolError(
            f"ipxe_lkrn_path {ipxe_lkrn_path!r} does not exist or is not a file. This module never fetches "
            "ipxe.lkrn itself -- see its own DOCUMENTATION on why -- so it must already be present locally.",
            operation="asmb8_bootstrap_image.build",
        )

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=work_dir, prefix="asmb8-bootstrap-") as staging:
        staging_root = _stage_directory(Path(staging), ipxe_lkrn_path=lkrn, script_text=script_text)

        argv = [grub_mkrescue_path, f"--output={output}", str(staging_root)]
        completed = run_command(argv, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            excerpt = redact(((completed.stderr or "") + "\n" + (completed.stdout or "")).strip())
            raise ProtocolError(
                f"grub-mkrescue exited {completed.returncode}: {excerpt[:_MAX_TOOL_OUTPUT_EXCERPT]}",
                operation="asmb8_bootstrap_image.build",
            )

    if not output.is_file():
        raise ProtocolError(
            f"grub-mkrescue reported success (rc=0) but {output_path!r} was not produced.",
            operation="asmb8_bootstrap_image.build",
        )

    size_bytes = output.stat().st_size
    if size_bytes > size_budget_bytes:
        output.unlink(missing_ok=True)
        raise ProtocolError(
            f"bootstrap image is {size_bytes} bytes, exceeding the {size_budget_bytes}-byte size budget. "
            "Refusing to leave an oversized 'bootstrap' image in place -- a bootstrap that silently grows "
            "toward the size of a full installer ISO recreates exactly the iUSB-throughput problem this "
            "module exists to avoid. The oversized file has been removed.",
            operation="asmb8_bootstrap_image.build",
        )

    return {"size_bytes": size_bytes, "output_path": str(output)}
