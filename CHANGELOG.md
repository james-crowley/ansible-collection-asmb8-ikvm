# james\_crowley\.asmb8\_ikvm Release Notes

**Topics**

- <a href="#v0-3-0">v0\.3\.0</a>
    - <a href="#release-summary">Release Summary</a>
    - <a href="#minor-changes">Minor Changes</a>
    - <a href="#bugfixes">Bugfixes</a>
    - <a href="#new-modules">New Modules</a>
- <a href="#v0-2-0">v0\.2\.0</a>
    - <a href="#release-summary-1">Release Summary</a>
    - <a href="#major-changes">Major Changes</a>
    - <a href="#minor-changes-1">Minor Changes</a>
    - <a href="#bugfixes-1">Bugfixes</a>
- <a href="#v0-1-0">v0\.1\.0</a>
    - <a href="#release-summary-2">Release Summary</a>
    - <a href="#new-modules-1">New Modules</a>

<a id="v0-3-0"></a>
## v0\.3\.0

<a id="release-summary"></a>
### Release Summary

Adds the two modules that 0\.2\.0 documented but did not ship\, plus a
substantially rewritten README\.

<code>asmb8\_reset</code> cold\- or warm\-resets the BMC\'s management controller over
standard IPMI\. It is the recovery path for a wedged virtual\-media session\,
which this board makes possible because <code>cd\-media</code> allows exactly one
session with no server\-side timeout to reclaim an abandoned one\. Confirmed
against real hardware\: the reset is accepted and the host stays powered and
unaffected\.

<code>asmb8\_http\_origin</code> runs an ephemeral\, path\-confined\, lifetime\-capped local
HTTP file server\. It exists for installers that can fetch bulk files over
LAN\-speed HTTP rather than the much slower iUSB virtual\-media path\, and it is
built so that it cannot outlive the play that started it even if that play
crashes or is interrupted\.

The <code>asmb8\_baremetal\_install</code> role\'s handoff timeout was hardcoded at 60
minutes\, which truncated a real install that needed longer\. It is now
configurable\, defaults generously\, and the role\'s README shows the arithmetic
so a user can size it for their own image rather than guess\.

This release remains <strong>not hardware\-qualified</strong>\. A completed unattended
operating\-system install is still unproven — the furthest attempt reached 70\%
before virtual\-CD read timeouts stopped it — IPMI Serial\-over\-LAN does not work
on the one board tested\, and <code>asmb8\_console</code> still has no live\-hardware or
mock coverage of its handshake state machine\. <code>README\.md</code> and
<code>docs/capability\-matrix\.md</code> give the claim\-by\-claim accounting\, and
<code>docs/hardware\-evidence\-2026\-08\-08\.md</code> records the dated observations behind
every claim\, including the negative results\.

<a id="minor-changes"></a>
### Minor Changes

* <code>asmb8\_media</code> \- add a regression test pinning that the KVM/media token is resolved from the JNLP by flag name rather than by argument position\. The shipped parser was already correct\; the test exists because a diagnostic harness read the token positionally as the fourth <code>\<argument\></code>\, which on this firmware is <code>\-hostname</code>\'s value \-\- the BMC\'s own IP address\. Authenticating with that made the BMC refuse redirection with an otherwise\-undocumented status <code>3</code>\, a failure whose only visible symptom was an established\-but\-idle socket\. Status <code>3</code> means \"bad token\"\. The test uses this firmware\'s real argument order so a future refactor back to index arithmetic fails in CI rather than on hardware\.
* asmb8\_http\_origin \- new module\, an ephemeral local HTTP file server for handing installer files to a target that only speaks HTTP rather than this collection\'s native iUSB path\. Kept to this collection\'s \"no standing infrastructure\" rule with a hard\, unconditional <code>lifetime\_seconds</code> cap so a background instance self\-terminates even if the play that started it crashes or the controller loses power mid\-run\; per\-request path confinement against <code>\.\.</code>\, symlink\, and percent\-encoding \(including double\-encoding\) traversal\; a structured per\-request access log for diagnosing a failed install\; and correct <code>Range</code>/<code>206 Partial Content</code> handling for bootloaders and installers that issue ranged reads\. Added to <code>meta/runtime\.yml</code>\'s <code>asmb8\_ikvm</code> action group alongside the other six modules\.
* asmb8\_media \- the background media session\'s state file now records idle\-streak bookkeeping \(<code>idle\_polls</code>\, <code>current\_idle\_streak</code>\, <code>last\_idle\_streak</code>\, <code>idle\_poll\_interval\_seconds</code>\) so a post\-mortem can separate a healthy\, expected idle period from a connection that actually broke\, instead of relying on a single point\-in\-time read of <code>updated\_at</code>\. Surfaced under <code>operation\.observed</code> on every <code>asmb8\_media</code> call\.
* asmb8\_reset \- new module for BMC cold/warm self\-reset over IPMI \(netfn <code>0x06</code> cmd <code>0x02</code>/<code>0x03</code>\)\, promoting the documented manual <code>ipmitool mc reset cold</code> recovery step to a first\-class\, testable module\. Destructive\-adjacent\: drops every active BMC session\, including any in\-flight virtual\-media session\, though the host itself is unaffected \(verified live\: the host stayed powered on and never rebooted across a real cold reset\)\. Supports full <code>check\_mode</code>\, which never opens an IPMI connection at all \-\- a self\-reset has no idempotent \"previous state\" to read\. Added to <code>meta/runtime\.yml</code>\'s <code>asmb8\_ikvm</code> action group\.

<a id="bugfixes"></a>
### Bugfixes

* asmb8\_baremetal\_install \- raise the default <code>asmb8\_baremetal\_install\_handoff\_timeout</code> from <code>3600</code> to <code>7200</code> seconds\. A real\, unattended Proxmox install against the target hardware was killed by the old one\-hour default at 70\% complete\; see the role\'s README \(\"Expected duration\"\) for the sizing arithmetic and how to compute a value for your own ISO\.

<a id="new-modules"></a>
### New Modules

* james\_crowley\.asmb8\_ikvm\.asmb8\_http\_origin \- Run \(or stop\) an ephemeral\, lifetime\-capped local HTTP file server
* james\_crowley\.asmb8\_ikvm\.asmb8\_reset \- Reset the ASMB8\-iKVM BMC\'s management controller over IPMI

<a id="v0-2-0"></a>
## v0\.2\.0

<a id="release-summary-1"></a>
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

<a id="minor-changes-1"></a>
### Minor Changes

* asmb8\_console \- new module\, carrying asmb8\_redirection\'s former IVTP console/KVM\-session implementation \(handshake\, <code>capture\=handshake\_only\|raw\_frame\|decoded\_frame</code>\) unchanged in behaviour\. Added to <code>meta/runtime\.yml</code>\'s <code>asmb8\_ikvm</code> action group alongside the other five modules\.

<a id="bugfixes-1"></a>
### Bugfixes

* <code>asmb8\_media</code> \- disable Nagle\'s algorithm \(<code>TCP\_NODELAY</code>\) on the iUSB media socket\, which Python\'s <code>socket\.create\_connection</code> leaves enabled by default\. iUSB is strictly synchronous request/response\, the traffic shape Nagle handles worst\: each reply is written in a single <code>sendall</code>\, but its final partial segment was previously withheld pending an ACK the peer delays\. This is the theoretically\-correct default for this traffic shape regardless of measured impact\. A controlled A/B test on real hardware \(same install\, same ISO\, same machine\, with and without the option\) found no measurable throughput difference \- the two runs matched within about 2\% throughout and were byte\-identical from roughly the 3\.5\-minute mark onward\. Recorded as a negative result\: this option was not the install\'s latency bottleneck on this hardware\, and should not be re\-investigated for that reason without new evidence\. See <code>docs/hardware\-evidence\-2026\-08\-08\.md</code>\.
* <code>asmb8\_media</code> \- the emulated CD\-ROM\'s <code>READ TOC</code> handler ignored the SCSI CDB\'s allocation length and always returned its full 20\-byte response\. A Linux initrd requesting 12 bytes received 20\, which is a protocol violation\; the host\'s optical layer concluded the disc had no valid track structure\, never read the ISO9660 superblock\, and reported no valid installation medium\. Only a real operating system is affected \- bootloaders read via firmware I/O and never issue <code>READ TOC</code>\, so booting an installer appeared to work while the subsequent handoff to the OS always failed\. The handler now truncates to the allocation length while still reporting the full available length in the TOC data\-length field\, so an initiator that under\-allocated can retry with a larger buffer\.

<a id="v0-1-0"></a>
## v0\.1\.0

<a id="release-summary-2"></a>
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

<a id="new-modules-1"></a>
### New Modules

* james\_crowley\.asmb8\_ikvm\.asmb8\_boot \- Select a one\-time IPMI boot device on an ASMB8\-iKVM endpoint
* james\_crowley\.asmb8\_ikvm\.asmb8\_info \- Gather ASMB8\-iKVM capability and state facts
* james\_crowley\.asmb8\_ikvm\.asmb8\_media \- Attach or detach a local ISO to an ASMB8\-iKVM virtual CD\-ROM over iUSB
* james\_crowley\.asmb8\_ikvm\.asmb8\_power \- Control and query ASMB8\-iKVM power state over IPMI
* james\_crowley\.asmb8\_ikvm\.asmb8\_redirection \- Open an ASMB8\-iKVM console/KVM \(IVTP\) session headlessly
