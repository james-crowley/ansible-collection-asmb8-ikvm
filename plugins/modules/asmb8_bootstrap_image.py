#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_bootstrap_image
short_description: Build a size-budgeted, bootable iPXE bootstrap image that chains to an HTTP origin
description:
  - >-
    Builds a small, bootable ISO carrying a prebuilt iPXE binary and an embedded script that brings
    up the target's real NIC (O(network_mode)) and C(chain)s to O(origin_url) -- typically an
    ephemeral session started by M(james_crowley.asmb8_ikvm.asmb8_http_origin). Attaching this image
    over iUSB instead of a full installer ISO is this collection's fix for
    C(docs/hardware-evidence-2026-08-08.md)'s measured ~790-900 KB/s iUSB throughput: the bulk
    installer transfer moves over plain HTTP at LAN speed instead, entirely bypassing the BMC. See
    C(docs/netboot-design.md) for the research this module implements.
  - >-
    B(No compiler, and no container runtime, at Ansible run time.) C(docs/netboot-design.md) section 4
    rules out iPXE's own C(EMBED=) build parameter specifically because it needs a full C toolchain
    present -- exactly the standing-infrastructure dependency this collection otherwise avoids. This
    module instead wraps a prebuilt C(ipxe.lkrn) (a static ~382 KiB binary shaped so GRUB loads it
    exactly like a Linux kernel) with a GRUB2 configuration built by C(grub-mkrescue), passing the
    embedded script as that "kernel"'s initrd rather than compiling it in -- iPXE's own documented,
    no-rebuild alternative to C(EMBED=). B(The trade-off, stated honestly): a Docker-based
    C(make bin/ipxe.iso EMBED=...) path (also described in C(docs/netboot-design.md)) needs no local
    GRUB/xorriso install, but assumes a container runtime is present. A crash-cart laptop carried to a
    site with no other infrastructure may well not have Docker; C(grub-mkrescue)/C(xorriso) are
    ordinary Debian/Ubuntu packages with no daemon and no image pull, which is why this module chose
    that path as its only implementation rather than offering both.
  - >-
    B(This module never fetches C(ipxe.lkrn) itself.) O(ipxe_lkrn_path) must already exist on the
    controller -- caching that one small, static file is the caller's job (a C(get_url) task, a
    pre-staged artifact, whatever fits the environment), so this module makes no network request of
    any kind and has no hidden dependency on internet access from wherever it runs.
  - >-
    B(Enforces a hard size budget, and fails -- deleting the oversized result -- if the built image
    exceeds it.) A "bootstrap" that silently grows toward the size of a full installer ISO recreates
    exactly the iUSB-throughput problem this module exists to avoid. O(size_budget_bytes) defaults to
    16 MiB, matching a sibling homelab design's own limit; C(docs/netboot-design.md)'s own hand-built
    reference image measured 0.89 MiB, comfortably inside it. C(docs/netboot-design.md) section 4 is
    explicit that it never actually ran C(grub-mkrescue) to measure a real result -- this budget is
    what turns that document's "probably small" expectation into "provably small, or refused",
    without this module having to trust the estimate.
  - >-
    B(Static IP by default, not DHCP.) C(docs/netboot-design.md) section 5 documents that iPXE aborts
    a script on any failing line and explicitly recommends never emitting a bare C(dhcp) command for
    that reason; the brief this module was built against records an earlier attempt that failed to
    obtain a DHCP lease on this project's own network, which is exactly why a sibling homelab design
    bakes in a static address/netmask/gateway tuple instead. O(network_mode=static) (the default)
    requires O(address)/O(netmask)/O(gateway); O(network_mode=dhcp) is offered as an explicit
    caller opt-out for a network known to have working DHCP, never as this module's own
    recommendation.
  - >-
    B(What remains unverified, stated as plainly as C(docs/netboot-design.md) states its own open
    questions.) That document found only a legacy-GRUB (C(kernel)/C(initrd)) worked example for
    embedding a script alongside C(ipxe.lkrn) and explicitly flagged the GRUB2 translation as
    unconfirmed. This module renders GRUB2's C(linux16)/C(initrd16) commands (the 16-bit real-mode
    Linux boot protocol) rather than plain C(linux)/C(initrd) (GRUB2's newer 32/64-bit protocol
    path) -- the same choice real-world GRUB2 configurations make for C(memtest86+), another
    non-Linux payload that reuses the Linux kernel image format the way C(ipxe.lkrn) does. This is
    this module's own best-effort translation, not something C(docs/netboot-design.md) verified, and
    it has never been run against a real C(grub-mkrescue) or booted on real hardware -- see this
    collection's C(docs/capability-matrix.md) for where this lands in that document's tiers.
  - >-
    Detects the absence of O(grub_mkrescue_path)/O(xorriso_path) (C(grub-mkrescue)'s own internal
    dependency, checked by name so a missing install is diagnosed clearly rather than only surfacing
    once C(grub-mkrescue) itself fails in a way that names it) B(before) touching the filesystem, and
    fails with an actionable message naming the Debian/Ubuntu packages to install, rather than letting
    a missing tool surface as a bare traceback.
version_added: 0.5.0
author:
  - Jim Crowley (@james-crowley)
options:
  origin_url:
    description:
      - >-
        URL the built image's embedded script C(chain)s to once its NIC is up -- typically the
        C(url) returned by a M(james_crowley.asmb8_ikvm.asmb8_http_origin) session, plus whatever
        script name that origin is serving (e.g. a Proxmox-generated C(boot.ipxe) -- see
        C(docs/netboot-design.md) section 3). This module has no opinion about what O(origin_url)
        actually serves; its only job is bringing up the NIC and hand off.
    type: str
    required: true
  ipxe_lkrn_path:
    description:
      - >-
        Path, on the Ansible controller, to a prebuilt C(ipxe.lkrn) binary. B(Never fetched by this
        module) -- see the module description. Measured directly against C(boot.ipxe.org)'s public
        download at 391,065 bytes as of the research in C(docs/netboot-design.md); cacheable, since
        it needs no rebuild for a different O(origin_url) or network configuration.
    type: path
    required: true
  output_path:
    description: Path, on the Ansible controller, to write the built bootstrap ISO to.
    type: path
    required: true
  network_mode:
    description:
      - >-
        V(static) (the default) requires O(address)/O(netmask)/O(gateway) and renders C(set net0/ip
        ...)/C(ifopen net0) lines, never a bare C(dhcp) command. V(dhcp) renders a bare C(dhcp)
        command instead -- an explicit caller opt-out for a network known to have working DHCP, not
        this module's own recommendation. See the module description for why V(static) is the
        default here.
    type: str
    choices: [static, dhcp]
    default: static
  address:
    description: Static IPv4/IPv6 address for the target's NIC. Required when O(network_mode=static).
    type: str
  netmask:
    description: Static netmask. Required when O(network_mode=static).
    type: str
  gateway:
    description: Static default gateway. Required when O(network_mode=static).
    type: str
  dns:
    description: >-
      Optional static DNS server. C(docs/netboot-design.md) section 5 notes this design's every
      fetch target is a hardcoded IP address, never a hostname, so this is rendered when given but
      not required even when O(network_mode=static).
    type: str
  size_budget_bytes:
    description: Hard cap, in bytes, on the built image's size. See the module description.
    type: int
    default: 16777216
  grub_mkrescue_path:
    description: >-
      Explicit path to C(grub-mkrescue), overriding a C(PATH) search. Trusted outright if it exists
      and is executable; never searched for beyond the literal path given.
    type: path
  xorriso_path:
    description: >-
      Explicit path to C(xorriso), overriding a C(PATH) search. C(grub-mkrescue) depends on
      C(xorriso) internally; this module checks for it by name up front purely so a missing install
      is diagnosed clearly, even though this module never invokes C(xorriso) directly itself.
    type: path
  work_dir:
    description: Parent directory for this module's temporary staging directory. Defaults to the system temp directory.
    type: path
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_http_origin
  - module: james_crowley.asmb8_ikvm.asmb8_media
attributes:
  check_mode:
    description: >-
      Supported. Validates every option, confirms O(ipxe_lkrn_path) exists, and confirms
      C(grub-mkrescue)/C(xorriso) can be found -- but never invokes C(grub-mkrescue) and never writes
      O(output_path). Because nothing is actually built, check mode cannot confirm the result would
      fit O(size_budget_bytes); RV(size_bytes) is V(null) in check mode for exactly this reason.
    support: full
  diff_mode:
    description: Not supported. Use RV(size_bytes)/RV(output_path) instead.
    support: none
"""

EXAMPLES = r"""
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
"""

RETURN = r"""
changed:
  description: V(true) whenever a build was (or, in check mode, would be) performed. This module has no idempotent no-op case.
  type: bool
  returned: always
output_path:
  description: Same value as O(output_path), for convenience after a build.
  type: str
  returned: always
size_bytes:
  description: >-
    The built image's actual size in bytes. V(null) in check mode, since nothing was actually built --
    see this module's C(check_mode) attribute note.
  type: int
  returned: when available
size_budget_bytes:
  description: The effective O(size_budget_bytes) enforced for this build.
  type: int
  returned: always
script:
  description: The embedded iPXE script this build rendered, for diagnosis.
  type: str
  returned: always
grub_mkrescue_path:
  description: The resolved C(grub-mkrescue) path actually used.
  type: str
  returned: when available
operation:
  description: >-
    The C(asmb8-ikvm-operation/v1) receipt for this build, in the same nested shape every mutating
    module in this collection returns it under. RV(operation.endpoint) is O(output_path) -- this
    module never contacts a BMC, so there is no BMC endpoint to report.
  type: dict
  returned: always
  contains:
    schema:
      description: Always V(asmb8-ikvm-operation/v1).
      type: str
    action:
      description: Always V(asmb8_bootstrap_image.build).
      type: str
    endpoint:
      description: Same value as O(output_path).
      type: str
    changed:
      description: Mirrors the top-level RV(changed).
      type: bool
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import bootstrap_image
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt


def argument_spec() -> dict[str, dict]:
    return {
        "origin_url": {"type": "str", "required": True},
        "ipxe_lkrn_path": {"type": "path", "required": True},
        "output_path": {"type": "path", "required": True},
        "network_mode": {"type": "str", "choices": list(bootstrap_image.NETWORK_MODES), "default": bootstrap_image.NETWORK_MODE_STATIC},
        "address": {"type": "str"},
        "netmask": {"type": "str"},
        "gateway": {"type": "str"},
        "dns": {"type": "str"},
        "size_budget_bytes": {"type": "int", "default": bootstrap_image.DEFAULT_SIZE_BUDGET_BYTES},
        "grub_mkrescue_path": {"type": "path"},
        "xorriso_path": {"type": "path"},
        "work_dir": {"type": "path"},
    }


def build_network_config(params: dict) -> bootstrap_image.NetworkConfig:
    return bootstrap_image.NetworkConfig(
        mode=params["network_mode"],
        address=params.get("address"),
        netmask=params.get("netmask"),
        gateway=params.get("gateway"),
        dns=params.get("dns"),
    )


def resolve_required_tools(params: dict) -> tuple[str | None, str | None]:
    """Resolve grub-mkrescue and xorriso, returning ``(grub_mkrescue_path, xorriso_path)``.

    Either element is ``None`` when that tool could not be found -- see
    :func:`bootstrap_image.find_tool`. Callers turn a ``None`` into
    :func:`bootstrap_image.missing_tool_error` rather than proceeding.
    """
    grub_mkrescue = bootstrap_image.find_tool("grub-mkrescue", explicit_path=params.get("grub_mkrescue_path"))
    xorriso = bootstrap_image.find_tool("xorriso", explicit_path=params.get("xorriso_path"))
    return grub_mkrescue, xorriso


def main() -> None:
    module = AnsibleModule(
        argument_spec=argument_spec(),
        required_if=[("network_mode", bootstrap_image.NETWORK_MODE_STATIC, ["address", "netmask", "gateway"])],
        supports_check_mode=True,
    )
    params = module.params

    network = build_network_config(params)
    script_text = bootstrap_image.render_ipxe_script(origin_url=params["origin_url"], network=network)

    grub_mkrescue_path, xorriso_path = resolve_required_tools(params)
    missing = [name for name, path in (("grub-mkrescue", grub_mkrescue_path), ("xorriso", xorriso_path)) if path is None]
    if missing:
        err = bootstrap_image.missing_tool_error(missing)
        module.fail_json(**err.to_result(), script=script_text)
        return

    endpoint = params["output_path"]

    if module.check_mode:
        receipt = OperationReceipt(action="asmb8_bootstrap_image.build", endpoint=endpoint, changed=True, desired="built")
        module.exit_json(
            changed=True,
            output_path=params["output_path"],
            size_bytes=None,
            size_budget_bytes=params["size_budget_bytes"],
            script=script_text,
            grub_mkrescue_path=grub_mkrescue_path,
            operation=receipt.to_dict(),
        )
        return

    try:
        result = bootstrap_image.build_bootstrap_image(
            ipxe_lkrn_path=params["ipxe_lkrn_path"],
            script_text=script_text,
            output_path=params["output_path"],
            size_budget_bytes=params["size_budget_bytes"],
            grub_mkrescue_path=grub_mkrescue_path,
            work_dir=params.get("work_dir"),
        )
    except IkvmError as err:
        module.fail_json(**err.to_result(), script=script_text)
        return

    receipt = OperationReceipt(action="asmb8_bootstrap_image.build", endpoint=endpoint, changed=True, desired="built", observed=result)
    module.exit_json(
        changed=True,
        output_path=result["output_path"],
        size_bytes=result["size_bytes"],
        size_budget_bytes=params["size_budget_bytes"],
        script=script_text,
        grub_mkrescue_path=grub_mkrescue_path,
        operation=receipt.to_dict(),
    )


if __name__ == "__main__":
    main()
