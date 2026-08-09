# james\_crowley\.asmb8\_ikvm Release Notes

**Topics**

- <a href="#v0-2-0">v0\.2\.0</a>
    - <a href="#release-summary">Release Summary</a>
    - <a href="#major-changes">Major Changes</a>
    - <a href="#minor-changes">Minor Changes</a>
    - <a href="#bugfixes">Bugfixes</a>
- <a href="#v0-1-0">v0\.1\.0</a>
    - <a href="#release-summary-1">Release Summary</a>
    - <a href="#new-modules">New Modules</a>

<a id="v0-2-0"></a>
## v0\.2\.0

<a id="release-summary"></a>
### Release Summary

First release intended for Ansible Galaxy\. <code>0\.1\.0</code> was tagged but never
published\, and two changes since then would have been breaking or misleading
to ship\, so this supersedes it rather than amending it\.

The headline change is a protocol fix\. The emulated CD\-ROM\'s <code>READ TOC</code>
handler violated the SCSI allocation\-length contract\, which made booting an
installer appear to work while every subsequent handoff to a real operating
system failed to find valid installation media\. Bootloaders never issue
<code>READ TOC</code>\, which is precisely why the fault stayed hidden through several
apparently\-successful boots\. That fix is confirmed on real hardware\: a run
with it in place streamed past the point where four earlier attempts stopped
dead\.

<code>asmb8\_redirection</code> has also been rewritten to match the name it was
already using\, and its former console implementation moved to a new
<code>asmb8\_console</code> module\. Doing this before the first publication avoids a
breaking rename later\.

This release remains <strong>not hardware\-qualified</strong>\. A completed unattended
operating\-system install is still unproven\, IPMI Serial\-over\-LAN does not
work on the one board tested\, and <code>asmb8\_console</code> has no live\-hardware or
mock coverage of its handshake state machine\. <code>README\.md</code> and
<code>docs/capability\-matrix\.md</code> give the claim\-by\-claim accounting\, and
<code>docs/hardware\-evidence\-2026\-08\-08\.md</code> records the dated observations
behind every claim — including the negative results\, which are documented
deliberately so they are not re\-investigated without new evidence\.

<a id="major-changes"></a>
### Major Changes

* asmb8\_redirection \- rewritten before the first Galaxy release to actually match its name\. It previously opened an IVTP console/KVM session \(closer to what amt\_media does in the sibling james\_crowley\.intel\_amt collection than to what amt\_redirection does\)\; that implementation has moved\, essentially unchanged\, to the new asmb8\_console module\. The name asmb8\_redirection now does what the sibling collection\'s amt\_redirection does\: report\, and \(once a real RPC is confirmed\) toggle\, whether this BMC\'s own listed services \(web\, kvm\, cd\-media\, fd\-media\, hd\-media\, ssh\, telnet\) are enabled\, separately from whether each one\'s TCP port is actually reachable right now\. Without a <code>state</code> option it is read\-only\, exactly like the sibling module\; any <code>state</code> request currently fails with <code>error\_class\=unsupported\_capability</code>\, since no sourced RPC exists yet for toggling a service\'s enablement on this BMC \(see the module\'s own DOCUMENTATION and docs/asmb8\_redirection\.md\)\.

<a id="minor-changes"></a>
### Minor Changes

* asmb8\_console \- new module\, carrying asmb8\_redirection\'s former IVTP console/KVM\-session implementation \(handshake\, <code>capture\=handshake\_only\|raw\_frame\|decoded\_frame</code>\) unchanged in behaviour\. Added to <code>meta/runtime\.yml</code>\'s <code>asmb8\_ikvm</code> action group alongside the other five modules\.

<a id="bugfixes"></a>
### Bugfixes

* <code>asmb8\_media</code> \- disable Nagle\'s algorithm \(<code>TCP\_NODELAY</code>\) on the iUSB media socket\, which Python\'s <code>socket\.create\_connection</code> leaves enabled by default\. iUSB is strictly synchronous request/response\, the traffic shape Nagle handles worst\: each reply is written in a single <code>sendall</code>\, but its final partial segment was previously withheld pending an ACK the peer delays\. This is the theoretically\-correct default for this traffic shape regardless of measured impact\. A controlled A/B test on real hardware \(same install\, same ISO\, same machine\, with and without the option\) found no measurable throughput difference \- the two runs matched within about 2\% throughout and were byte\-identical from roughly the 3\.5\-minute mark onward\. Recorded as a negative result\: this option was not the install\'s latency bottleneck on this hardware\, and should not be re\-investigated for that reason without new evidence\. See <code>docs/hardware\-evidence\-2026\-08\-08\.md</code>\.
* <code>asmb8\_media</code> \- the emulated CD\-ROM\'s <code>READ TOC</code> handler ignored the SCSI CDB\'s allocation length and always returned its full 20\-byte response\. A Linux initrd requesting 12 bytes received 20\, which is a protocol violation\; the host\'s optical layer concluded the disc had no valid track structure\, never read the ISO9660 superblock\, and reported no valid installation medium\. Only a real operating system is affected \- bootloaders read via firmware I/O and never issue <code>READ TOC</code>\, so booting an installer appeared to work while the subsequent handoff to the OS always failed\. The handler now truncates to the allocation length while still reporting the full available length in the TOC data\-length field\, so an initiator that under\-allocated can retry with a larger buffer\.

<a id="v0-1-0"></a>
## v0\.1\.0

<a id="release-summary-1"></a>
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
