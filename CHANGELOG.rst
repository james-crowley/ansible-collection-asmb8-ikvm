========================================
james\_crowley.asmb8\_ikvm Release Notes
========================================

.. contents:: Topics

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
