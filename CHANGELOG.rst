========================================
james\_crowley.asmb8\_ikvm Release Notes
========================================

.. contents:: Topics

v0.3.0
======

Release Summary
---------------

Adds the two modules that 0.2.0 documented but did not ship, plus a
substantially rewritten README.

``asmb8_reset`` cold- or warm-resets the BMC's management controller over
standard IPMI. It is the recovery path for a wedged virtual-media session,
which this board makes possible because ``cd-media`` allows exactly one
session with no server-side timeout to reclaim an abandoned one. Confirmed
against real hardware: the reset is accepted and the host stays powered and
unaffected.

``asmb8_http_origin`` runs an ephemeral, path-confined, lifetime-capped local
HTTP file server. It exists for installers that can fetch bulk files over
LAN-speed HTTP rather than the much slower iUSB virtual-media path, and it is
built so that it cannot outlive the play that started it even if that play
crashes or is interrupted.

The ``asmb8_baremetal_install`` role's handoff timeout was hardcoded at 60
minutes, which truncated a real install that needed longer. It is now
configurable, defaults generously, and the role's README shows the arithmetic
so a user can size it for their own image rather than guess.

This release remains **not hardware-qualified**. A completed unattended
operating-system install is still unproven — the furthest attempt reached 70%
before virtual-CD read timeouts stopped it — IPMI Serial-over-LAN does not work
on the one board tested, and ``asmb8_console`` still has no live-hardware or
mock coverage of its handshake state machine. ``README.md`` and
``docs/capability-matrix.md`` give the claim-by-claim accounting, and
``docs/hardware-evidence-2026-08-08.md`` records the dated observations behind
every claim, including the negative results.

Minor Changes
-------------

- ``asmb8_media`` - add a regression test pinning that the KVM/media token is resolved from the JNLP by flag name rather than by argument position. The shipped parser was already correct; the test exists because a diagnostic harness read the token positionally as the fourth ``<argument>``, which on this firmware is ``-hostname``'s value -- the BMC's own IP address. Authenticating with that made the BMC refuse redirection with an otherwise-undocumented status ``3``, a failure whose only visible symptom was an established-but-idle socket. Status ``3`` means "bad token". The test uses this firmware's real argument order so a future refactor back to index arithmetic fails in CI rather than on hardware.
- asmb8_http_origin - new module, an ephemeral local HTTP file server for handing installer files to a target that only speaks HTTP rather than this collection's native iUSB path. Kept to this collection's "no standing infrastructure" rule with a hard, unconditional ``lifetime_seconds`` cap so a background instance self-terminates even if the play that started it crashes or the controller loses power mid-run; per-request path confinement against ``..``, symlink, and percent-encoding (including double-encoding) traversal; a structured per-request access log for diagnosing a failed install; and correct ``Range``/``206 Partial Content`` handling for bootloaders and installers that issue ranged reads. Added to ``meta/runtime.yml``'s ``asmb8_ikvm`` action group alongside the other six modules.
- asmb8_media - the background media session's state file now records idle-streak bookkeeping (``idle_polls``, ``current_idle_streak``, ``last_idle_streak``, ``idle_poll_interval_seconds``) so a post-mortem can separate a healthy, expected idle period from a connection that actually broke, instead of relying on a single point-in-time read of ``updated_at``. Surfaced under ``operation.observed`` on every ``asmb8_media`` call.
- asmb8_reset - new module for BMC cold/warm self-reset over IPMI (netfn ``0x06`` cmd ``0x02``/``0x03``), promoting the documented manual ``ipmitool mc reset cold`` recovery step to a first-class, testable module. Destructive-adjacent: drops every active BMC session, including any in-flight virtual-media session, though the host itself is unaffected (verified live: the host stayed powered on and never rebooted across a real cold reset). Supports full ``check_mode``, which never opens an IPMI connection at all -- a self-reset has no idempotent "previous state" to read. Added to ``meta/runtime.yml``'s ``asmb8_ikvm`` action group.

Bugfixes
--------

- asmb8_baremetal_install - raise the default ``asmb8_baremetal_install_handoff_timeout`` from ``3600`` to ``7200`` seconds. A real, unattended Proxmox install against the target hardware was killed by the old one-hour default at 70% complete; see the role's README ("Expected duration") for the sizing arithmetic and how to compute a value for your own ISO.

New Modules
-----------

- james_crowley.asmb8_ikvm.asmb8_http_origin - Run (or stop) an ephemeral, lifetime\-capped local HTTP file server
- james_crowley.asmb8_ikvm.asmb8_reset - Reset the ASMB8\-iKVM BMC's management controller over IPMI

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
