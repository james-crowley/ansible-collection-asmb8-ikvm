========================================
james\_crowley.asmb8\_ikvm Release Notes
========================================

.. contents:: Topics

v0.2.0
======

Release Summary
---------------

First release intended for Ansible Galaxy. ``0.1.0`` was tagged but never
published, and two changes since then would have been breaking or misleading
to ship, so this supersedes it rather than amending it.

The headline change is a protocol fix. The emulated CD-ROM's ``READ TOC``
handler violated the SCSI allocation-length contract, which made booting an
installer appear to work while every subsequent handoff to a real operating
system failed to find valid installation media. Bootloaders never issue
``READ TOC``, which is precisely why the fault stayed hidden through several
apparently-successful boots. That fix is confirmed on real hardware: a run
with it in place streamed past the point where four earlier attempts stopped
dead.

``asmb8_redirection`` has also been rewritten to match the name it was
already using, and its former console implementation moved to a new
``asmb8_console`` module. Doing this before the first publication avoids a
breaking rename later.

This release remains **not hardware-qualified**. A completed unattended
operating-system install is still unproven, IPMI Serial-over-LAN does not
work on the one board tested, and ``asmb8_console`` has no live-hardware or
mock coverage of its handshake state machine. ``README.md`` and
``docs/capability-matrix.md`` give the claim-by-claim accounting, and
``docs/hardware-evidence-2026-08-08.md`` records the dated observations
behind every claim — including the negative results, which are documented
deliberately so they are not re-investigated without new evidence.

Major Changes
-------------

- asmb8_redirection - rewritten before the first Galaxy release to actually match its name. It previously opened an IVTP console/KVM session (closer to what amt_media does in the sibling james_crowley.intel_amt collection than to what amt_redirection does); that implementation has moved, essentially unchanged, to the new asmb8_console module. The name asmb8_redirection now does what the sibling collection's amt_redirection does: report, and (once a real RPC is confirmed) toggle, whether this BMC's own listed services (web, kvm, cd-media, fd-media, hd-media, ssh, telnet) are enabled, separately from whether each one's TCP port is actually reachable right now. Without a ``state`` option it is read-only, exactly like the sibling module; any ``state`` request currently fails with ``error_class=unsupported_capability``, since no sourced RPC exists yet for toggling a service's enablement on this BMC (see the module's own DOCUMENTATION and docs/asmb8_redirection.md).

Minor Changes
-------------

- asmb8_console - new module, carrying asmb8_redirection's former IVTP console/KVM-session implementation (handshake, ``capture=handshake_only|raw_frame|decoded_frame``) unchanged in behaviour. Added to ``meta/runtime.yml``'s ``asmb8_ikvm`` action group alongside the other five modules.

Bugfixes
--------

- ``asmb8_media`` - disable Nagle's algorithm (``TCP_NODELAY``) on the iUSB media socket, which Python's ``socket.create_connection`` leaves enabled by default. iUSB is strictly synchronous request/response, the traffic shape Nagle handles worst: each reply is written in a single ``sendall``, but its final partial segment was previously withheld pending an ACK the peer delays. This is the theoretically-correct default for this traffic shape regardless of measured impact. A controlled A/B test on real hardware (same install, same ISO, same machine, with and without the option) found no measurable throughput difference - the two runs matched within about 2% throughout and were byte-identical from roughly the 3.5-minute mark onward. Recorded as a negative result: this option was not the install's latency bottleneck on this hardware, and should not be re-investigated for that reason without new evidence. See ``docs/hardware-evidence-2026-08-08.md``.
- ``asmb8_media`` - the emulated CD-ROM's ``READ TOC`` handler ignored the SCSI CDB's allocation length and always returned its full 20-byte response. A Linux initrd requesting 12 bytes received 20, which is a protocol violation; the host's optical layer concluded the disc had no valid track structure, never read the ISO9660 superblock, and reported no valid installation medium. Only a real operating system is affected - bootloaders read via firmware I/O and never issue ``READ TOC``, so booting an installer appeared to work while the subsequent handoff to the OS always failed. The handler now truncates to the allocation length while still reporting the full available length in the TOC data-length field, so an initiator that under-allocated can retry with a larger buffer.

v0.1.0
======

Release Summary
---------------

Initial pre-release. Out-of-band management of ASUS ASMB8-iKVM baseboard
management controllers from Ansible, including a native implementation of AMI's
proprietary iUSB virtual-media protocol so a local ISO can be streamed to a
bare-metal host's virtual CD-ROM with no PXE, DHCP, TFTP, NFS or CIFS
infrastructure.

This release is NOT hardware-qualified. See ``README.md`` for what has been
verified against real firmware and ``docs/hardware-evidence-2026-08-08.md`` for
the underlying observations, including an explicit list of capabilities that
remain unproven.

New Modules
-----------

- james_crowley.asmb8_ikvm.asmb8_boot - Select a one\-time IPMI boot device on an ASMB8\-iKVM endpoint
- james_crowley.asmb8_ikvm.asmb8_info - Gather ASMB8\-iKVM capability and state facts
- james_crowley.asmb8_ikvm.asmb8_media - Attach or detach a local ISO to an ASMB8\-iKVM virtual CD\-ROM over iUSB
- james_crowley.asmb8_ikvm.asmb8_power - Control and query ASMB8\-iKVM power state over IPMI
- james_crowley.asmb8_ikvm.asmb8_redirection - Open an ASMB8\-iKVM console/KVM (IVTP) session headlessly
