# james\_crowley\.asmb8\_ikvm Release Notes

**Topics**

- <a href="#v0-1-0">v0\.1\.0</a>
    - <a href="#release-summary">Release Summary</a>
    - <a href="#new-modules">New Modules</a>

<a id="v0-1-0"></a>
## v0\.1\.0

<a id="release-summary"></a>
### Release Summary

Initial pre\-release\. Out\-of\-band management of ASUS ASMB8\-iKVM baseboard
management controllers from Ansible\, including a native implementation of AMI\'s
proprietary iUSB virtual\-media protocol so a local ISO can be streamed to a
bare\-metal host\'s virtual CD\-ROM with no PXE\, DHCP\, TFTP\, NFS or CIFS
infrastructure\.

This release is NOT hardware\-qualified\. See <code>README\.md</code> for what has been
verified against real firmware and <code>docs/hardware\-evidence\-2026\-08\-08\.md</code> for
the underlying observations\, including an explicit list of capabilities that
remain unproven\.

<a id="new-modules"></a>
### New Modules

* james\_crowley\.asmb8\_ikvm\.asmb8\_boot \- Select a one\-time IPMI boot device on an ASMB8\-iKVM endpoint
* james\_crowley\.asmb8\_ikvm\.asmb8\_info \- Gather ASMB8\-iKVM capability and state facts
* james\_crowley\.asmb8\_ikvm\.asmb8\_media \- Attach or detach a local ISO to an ASMB8\-iKVM virtual CD\-ROM over iUSB
* james\_crowley\.asmb8\_ikvm\.asmb8\_power \- Control and query ASMB8\-iKVM power state over IPMI
* james\_crowley\.asmb8\_ikvm\.asmb8\_redirection \- Open an ASMB8\-iKVM console/KVM \(IVTP\) session headlessly
