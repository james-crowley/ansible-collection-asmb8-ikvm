========================================
james\_crowley.asmb8\_ikvm Release Notes
========================================

.. contents:: Topics

v0.4.0
======

Release Summary
---------------

Takes the collection from 8 modules to 18. The new surface is read-only: ten
modules that report what the BMC knows, none of which can change its
configuration.

Nine of them read the BMC's own ``.asp`` web-management interface, and every
endpoint they use is sourced from a capture of real hardware which is checked
in as test fixtures. ``asmb8_postcode`` deserves particular mention: it reads
the BIOS POST code, which is the only out-of-band view of boot progress this
board offers, since IPMI Serial-over-LAN does not work on it and the console's
video cannot be decoded. The tenth, ``asmb8_identify``, drives the chassis
identify LED over standard IPMI.

Credential-shaped values are never returned. Field extraction is allow-list
based, so a secret appearing on a firmware revision this project has not
captured is dropped by construction rather than relying on a filter to catch
it. Where the BMC exposes a secret, these modules report a boolean such as
``password_configured`` instead of the value. Audit log entries are the
deliberate exception and are returned verbatim, because sanitising a free-text
audit record would corrupt it and give false assurance.

Writes are described in each module's documentation and deliberately absent.
Changing users, network configuration or certificates can lock an operator out
of the BMC entirely, altering alert destinations can silently stop
notifications reaching anyone, and clearing an audit log destroys evidence.

Also in this release: an interrupted play no longer risks stranding the BMC's
single virtual-media slot, since the media daemon now records why it stopped
and its signal teardown is covered by a test that forks a real process and
signals it; a reusable mock IVTP server gives ``asmb8_console`` the protocol
coverage it lacked; and every module now has a reference page under ``docs/``.

Still **not hardware-qualified**. A completed unattended operating-system
install remains unproven, and none of the ten new modules has been run against
a live BMC — their evidence is real captured responses replayed through a
mocked transport, which is not the same thing. ``docs/capability-matrix.md``
tiers every claim, and ``docs/hardware-evidence-2026-08-08.md`` records the
dated observations behind them, including the negative results.

Minor Changes
-------------

- Add ``asmb8_identify``, a module that controls the ASMB8-iKVM chassis identify LED over standard IPMI (netfn ``0x00``, cmd ``0x04``), via ``pyghmi``'s ``Command.set_identify()``. Verified directly against ``pyghmi`` 1.6.19's installed source that this always resolves to the standard command on this board (American Megatrends has no entry in ``pyghmi``'s OEM ``oemmap``, so the lookup falls through to its generic handler, which is caught internally and never reaches this collection's caller). Supports turning the LED on for a bounded duration, on indefinitely, or off; refuses the two combinations that would silently contradict the requested state (``duration`` set alongside ``state=off``, and ``duration=0`` alongside ``state=on``) before opening any IPMI session. Standard IPMI Chassis Identify has no read-back command, so this module does not claim idempotence it cannot back up -- ``changed`` is always ``true`` on a real run, matching ``asmb8_reset``'s own honesty about the same kind of gap, and check mode never opens a connection at all. Carries no lockout risk -- it can only ever change whether a light is lit.
- Add ``asmb8_network``, a read-only module reporting per-channel LAN configuration, the LAN-channel-to-interface mapping, DNS configuration, and NIC-bonding configuration (``getalllancfg.asp``, ``getlanchannelinfo.asp``, ``getdnscfg.asp``, ``getnwbondcfg.asp``, ``checknwbond.asp``). DNS TSIG key material is never returned, only a boolean indicating whether a key is configured. No write capability is implemented -- a mistaken network configuration change can sever the management path this collection uses to reach the board at all.
- Add ``asmb8_postcode``, a read-only module that reads the current BIOS POST code from ``getpostcode.asp`` and, with ``sample=true``, polls it repeatedly over a bounded duration to return the observed sequence and the distinct codes seen. With IPMI Serial-over-LAN confirmed dead on this board and ``asmb8_console`` unable to decode video, this is this collection's only remote signal of boot progress. Reports raw POST code values only -- no code-to-meaning table is included or invented, since none is sourced for this BIOS. Sampling is bounded and serialized on purpose, given this BMC's HTTP/1.0, no-keep-alive, 20-session-cap web server.
- Add ``asmb8_sel``, a read-only module that reads the BMC's System Event Log over the ``.asp`` web interface (``getallselentries.asp``, ``getmaxselentries.asp``, ``getselcfg.asp``), with an optional ``limit`` on the number of entries returned. Documents that the same log is also readable over plain IPMI via ``pyghmi``'s ``get_event_log()`` (confirmed against the target hardware, same 24 entries) and that IPMI is generally the better choice; this module exists for web-management-only reachability and cross-checking, not to duplicate that path. Strictly read-only -- no ``clear`` option exists or is planned on this module, and the paged ``getselentries.asp`` (POST) endpoint is deliberately not used, since ``AspClient.get_webvar()`` is GET-only by design.
- Add ``asmb8_sessions``, a read-only module reporting per-service session capacity and remote-session (KVM/media) configuration (``getallservicescfg.asp``, ``getremotesession.asp``). Session-count fields are decoded from this board's +128 offset encoding, confirmed against two independent capacity measurements already recorded elsewhere in this collection. A live directory of active sessions (``getsessioninfo.asp``) is not implemented, because that endpoint's real invocation requires ``POST`` and ``AspClient.get_webvar`` is deliberately ``GET``-only; this is a documented gap pending a POST-capable client method, not an oversight.
- Add ``asmb8_users``, a read-only module reporting local BMC user accounts, the current session's privilege role, and LDAP/AD role-group bindings (``getalluserinfo.asp``, ``getrole.asp``, ``getallrolegroupcfg.asp``). Empty user-table slots are reported as slots, not accounts; e-mail addresses and SSH key material are never returned, only booleans indicating whether they are configured; unsourced numeric privilege-limit fields are returned raw with an explicit caveat rather than decoded. No write capability is implemented -- account creation, modification, and role-group changes are deliberately out of scope, since a mistake there can lock an operator out of the BMC entirely.
- Add ``docs/asmb8_console.md``, matching the documentation page every other original module already has, and correct its own prior draft's claim that no unit/mock test exists for ``ivtp.py`` now that one does -- while being explicit that this remains Tier 2 (mock) evidence only -- every IVTP wire fact is still sourced from a decompiled vendor client, never from a live capture beyond the bare 8-byte greeting, and no console frame has ever been decoded from real hardware.
- Add ``plugins/module_utils/webvar.py``, a parser for AMI MegaRAC's ``.asp`` WEBVAR/JSONVAR response format (the JavaScript-object-literal shape returned by endpoints such as ``getdatetime.asp``, ``getallsensors.asp`` and ``getfwinfo.asp``), backed by a 54-file corpus of real, redacted responses captured from a live ASMB8-iKVM BMC (``tests/unit/fixtures/asp/``). This is shared foundation for planned read-only reporting modules and does not itself change any existing module's behavior.
- Add ``tests/integration/mock_servers/ivtp_server.py``, a deterministic mock IVTP KVM/console endpoint (the ``asmb8_console`` counterpart to ``IusbMockServer``), and drive ``plugins/module_utils/ivtp.py``'s real ``open_channel``/``capture_one_frame`` against it over loopback sockets in ``tests/unit/mock_servers/test_ivtp_server.py`` -- a full successful handshake, fragmented video-frame reassembly, a rejected session token, an unsolicited ``STOP_SESSION_IMMEDIATE`` both mid-handshake and mid-frame, a truncated header, a truncated body, and a ``pktSize`` that disagrees with the bytes actually sent. This closes the coverage gap ``docs/capability-matrix.md`` previously named explicitly -- ``asmb8_console``/``ivtp.py`` had zero unit/mock test coverage of the handshake state machine, the only module and ``module_utils`` file in this collection with neither. Also adds a stronger, real-server-backed regression for ``capture=decoded_frame``'s existing network-untouched-before-refusal behavior in ``tests/unit/plugins/modules/test_asmb8_console.py``.
- asmb8_alerts - add a new, read-only module that reads the ASMB8-iKVM BMC's alerting configuration (SMTP relay, LAN alert destinations, the IPMI PEF table, alert policies, email formatting, and per-policy last-triggered timestamps) over the C(.asp) web-management surface, grouped by what a caller wants to know rather than by endpoint name. Implements no writes -- see the module's own documentation for why changing alert destinations is deliberately out of scope for this release.
- asmb8_auditlog - add a new, read-only module that reads the ASMB8-iKVM BMC's audit log entries and its logging configuration (remote/SD-card mirroring, syslog enablement and rotation, and the IPMI SEL overflow policy) over the C(.asp) web-management surface. Entries are returned verbatim and are documented as potentially containing usernames or addresses. Implements no writes -- clearing the audit log is deliberately out of scope for this release.
- asmb8_inventory - new read-only module for ``getfwinfo.asp``, ``getprojectcfg.asp``, and ``getfruinfo.asp``. Decodes ``getfwinfo.asp``'s ``FirmwareRevision2`` as BCD (``20`` decimal is ``0x14`` hex, i.e. "14", not "20") so ``firmware.firmware_version`` renders as ``1.14`` -- matching this board's own "firmware 1.14, aux 1.14.2" as independently recorded in ``docs/protocol-notes.md`` and ``README.md`` -- and never as the wrong ``1.20``. Combines the three ``MfgID_*`` bytes into a little-endian manufacturer id without asserting which organisation it belongs to, since no sourced lookup for that value exists. Reports ``getprojectcfg.asp``'s full 42-entry ``FEATURES`` list verbatim (including its two real duplicates) alongside a deduplicated ``feature_set``, correcting an assumption in this module's original task brief that the fixture only carries one feature. Reports ``getfruinfo.asp`` honestly as unpopulated on the target board (one of the corpus's five sentinel-only fixtures) rather than inventing a normalized shape with no populated sample to source it from. Added to ``meta/runtime.yml``'s ``asmb8_ikvm`` action group.
- asmb8_sensors - new read-only module for ``getallsensors.asp`` (48 real sensor records on the target board). Scales ``SensorReading`` by the empirically-derived factor of 1000 into ``reading.value`` (cross-checked against nominal rail voltages, a plausible CPU temperature, and a plausible fan speed in the fixture) rather than presenting ``RawReading`` directly -- the voltage sensors in particular prove ``RawReading`` alone is not a usable value. Splits sensors into ``threshold`` (a real analog reading) versus ``discrete`` (event/state-only, where ``SensorReading`` has been observed to carry a placeholder rather than a real value) using each record's own ``SettableReadableFlags`` field. Decodes ``sensor_type``/reading unit from the standard IPMI specification's Sensor Type/Unit Type Codes tables, leaving the vendor-OEM sensor type range unmapped rather than inventing a name for it. Supports filtering by sensor name/type and always groups the result by decoded type name. Documents how this differs from reading sensors generically over IPMI via ``pyghmi`` (already a dependency of this collection) and when to prefer each. Added to ``meta/runtime.yml``'s ``asmb8_ikvm`` action group.
- roles/asmb8_baremetal_install - document that interrupting a play (not only a hard C(kill -9)) can strand the BMC's virtual-media slot on firmware that predates this release's ``SIGTERM`` fix, what the symptom looks like (an C(ESTABLISHED) TCP connection to port 5120 with unread bytes and zero SCSI commands serviced, and no C(vmedia: redirection accepted) log line), and that O(james_crowley.asmb8_ikvm.asmb8_reset) is the recovery path when software reclamation cannot see the session holding it.

Bugfixes
--------

- asmb8_media - fix a defect where an uncleanly-terminated background media-session daemon (a ``SIGTERM``, e.g. from an interrupted play or a stray ``pkill``, previously arriving with no guaranteed effect) could leave the BMC's single, board-wide C(cd-media) slot held forever -- C(getallservicescfg.asp) confirms this BMC applies no server-side timeout at all (C(SERVICE_TIMEOUT: 4294967295)) to reclaim it. The background daemon's existing ``SIGTERM`` handler is now proven, by a test that forks a real daemon process and sends it a real ``SIGTERM``, to route through the exact same normal-exit teardown a local O(state=detached) call already used -- closing the iUSB session (which sends the TCP C(FIN) the BMC needs to free the slot) before the process exits -- rather than merely having a handler registered. Deliberately not ``SIGINT``: a backgrounded process inherits C(SIG_IGN) for that signal from its launching shell's own job control, so C(kill -INT) on one is silently swallowed. The signal handler itself only ever sets a flag (never touches the network, a lock, or any other non-signal-safe state), which also makes it naturally idempotent -- a second ``SIGTERM`` arriving mid-shutdown is a no-op, not a hang or an exception.
- asmb8_media - the background daemon's state file, and RV(operation.observed.stop_reason), now record WHY a O(state=detached) session actually stopped (V(signal), V(peer_closed), or V(bmc_terminate)), mirroring O(james_crowley.asmb8_ikvm.asmb8_http_origin)'s identically-named field, so a post-mortem can tell a signalled stop apart from a BMC-initiated one.

New Modules
-----------

- james_crowley.asmb8_ikvm.asmb8_alerts - Read the ASMB8\-iKVM BMC's alerting configuration (SMTP, PEF, policies, LAN destinations)
- james_crowley.asmb8_ikvm.asmb8_auditlog - Read the ASMB8\-iKVM BMC's audit log and its logging configuration
- james_crowley.asmb8_ikvm.asmb8_identify - Control the ASMB8\-iKVM chassis identify LED over standard IPMI
- james_crowley.asmb8_ikvm.asmb8_inventory - Read ASMB8\-iKVM firmware, FRU, and project\-feature inventory over :literal:`.asp`
- james_crowley.asmb8_ikvm.asmb8_sensors - Read ASMB8\-iKVM sensor readings over the :literal:`.asp` web\-management surface

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
