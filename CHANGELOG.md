# james\_crowley\.asmb8\_ikvm Release Notes

**Topics**

- <a href="#v0-4-0">v0\.4\.0</a>
    - <a href="#release-summary">Release Summary</a>
    - <a href="#minor-changes">Minor Changes</a>
    - <a href="#bugfixes">Bugfixes</a>
    - <a href="#new-modules">New Modules</a>
- <a href="#v0-3-0">v0\.3\.0</a>
    - <a href="#release-summary-1">Release Summary</a>
    - <a href="#minor-changes-1">Minor Changes</a>
    - <a href="#bugfixes-1">Bugfixes</a>
    - <a href="#new-modules-1">New Modules</a>
- <a href="#v0-2-0">v0\.2\.0</a>
    - <a href="#release-summary-2">Release Summary</a>
    - <a href="#major-changes">Major Changes</a>
    - <a href="#minor-changes-2">Minor Changes</a>
    - <a href="#bugfixes-2">Bugfixes</a>
- <a href="#v0-1-0">v0\.1\.0</a>
    - <a href="#release-summary-3">Release Summary</a>
    - <a href="#new-modules-2">New Modules</a>

<a id="v0-4-0"></a>
## v0\.4\.0

<a id="release-summary"></a>
### Release Summary

Takes the collection from 8 modules to 18\. The new surface is read\-only\: ten
modules that report what the BMC knows\, none of which can change its
configuration\.

Nine of them read the BMC\'s own <code>\.asp</code> web\-management interface\, and every
endpoint they use is sourced from a capture of real hardware which is checked
in as test fixtures\. <code>asmb8\_postcode</code> deserves particular mention\: it reads
the BIOS POST code\, which is the only out\-of\-band view of boot progress this
board offers\, since IPMI Serial\-over\-LAN does not work on it and the console\'s
video cannot be decoded\. The tenth\, <code>asmb8\_identify</code>\, drives the chassis
identify LED over standard IPMI\.

Credential\-shaped values are never returned\. Field extraction is allow\-list
based\, so a secret appearing on a firmware revision this project has not
captured is dropped by construction rather than relying on a filter to catch
it\. Where the BMC exposes a secret\, these modules report a boolean such as
<code>password\_configured</code> instead of the value\. Audit log entries are the
deliberate exception and are returned verbatim\, because sanitising a free\-text
audit record would corrupt it and give false assurance\.

Writes are described in each module\'s documentation and deliberately absent\.
Changing users\, network configuration or certificates can lock an operator out
of the BMC entirely\, altering alert destinations can silently stop
notifications reaching anyone\, and clearing an audit log destroys evidence\.

Also in this release\: an interrupted play no longer risks stranding the BMC\'s
single virtual\-media slot\, since the media daemon now records why it stopped
and its signal teardown is covered by a test that forks a real process and
signals it\; a reusable mock IVTP server gives <code>asmb8\_console</code> the protocol
coverage it lacked\; and every module now has a reference page under <code>docs/</code>\.

Still <strong>not hardware\-qualified</strong>\. A completed unattended operating\-system
install remains unproven\, and none of the ten new modules has been run against
a live BMC — their evidence is real captured responses replayed through a
mocked transport\, which is not the same thing\. <code>docs/capability\-matrix\.md</code>
tiers every claim\, and <code>docs/hardware\-evidence\-2026\-08\-08\.md</code> records the
dated observations behind them\, including the negative results\.

<a id="minor-changes"></a>
### Minor Changes

* Add <code>asmb8\_identify</code>\, a module that controls the ASMB8\-iKVM chassis identify LED over standard IPMI \(netfn <code>0x00</code>\, cmd <code>0x04</code>\)\, via <code>pyghmi</code>\'s <code>Command\.set\_identify\(\)</code>\. Verified directly against <code>pyghmi</code> 1\.6\.19\'s installed source that this always resolves to the standard command on this board \(American Megatrends has no entry in <code>pyghmi</code>\'s OEM <code>oemmap</code>\, so the lookup falls through to its generic handler\, which is caught internally and never reaches this collection\'s caller\)\. Supports turning the LED on for a bounded duration\, on indefinitely\, or off\; refuses the two combinations that would silently contradict the requested state \(<code>duration</code> set alongside <code>state\=off</code>\, and <code>duration\=0</code> alongside <code>state\=on</code>\) before opening any IPMI session\. Standard IPMI Chassis Identify has no read\-back command\, so this module does not claim idempotence it cannot back up \-\- <code>changed</code> is always <code>true</code> on a real run\, matching <code>asmb8\_reset</code>\'s own honesty about the same kind of gap\, and check mode never opens a connection at all\. Carries no lockout risk \-\- it can only ever change whether a light is lit\.
* Add <code>asmb8\_network</code>\, a read\-only module reporting per\-channel LAN configuration\, the LAN\-channel\-to\-interface mapping\, DNS configuration\, and NIC\-bonding configuration \(<code>getalllancfg\.asp</code>\, <code>getlanchannelinfo\.asp</code>\, <code>getdnscfg\.asp</code>\, <code>getnwbondcfg\.asp</code>\, <code>checknwbond\.asp</code>\)\. DNS TSIG key material is never returned\, only a boolean indicating whether a key is configured\. No write capability is implemented \-\- a mistaken network configuration change can sever the management path this collection uses to reach the board at all\.
* Add <code>asmb8\_postcode</code>\, a read\-only module that reads the current BIOS POST code from <code>getpostcode\.asp</code> and\, with <code>sample\=true</code>\, polls it repeatedly over a bounded duration to return the observed sequence and the distinct codes seen\. With IPMI Serial\-over\-LAN confirmed dead on this board and <code>asmb8\_console</code> unable to decode video\, this is this collection\'s only remote signal of boot progress\. Reports raw POST code values only \-\- no code\-to\-meaning table is included or invented\, since none is sourced for this BIOS\. Sampling is bounded and serialized on purpose\, given this BMC\'s HTTP/1\.0\, no\-keep\-alive\, 20\-session\-cap web server\.
* Add <code>asmb8\_sel</code>\, a read\-only module that reads the BMC\'s System Event Log over the <code>\.asp</code> web interface \(<code>getallselentries\.asp</code>\, <code>getmaxselentries\.asp</code>\, <code>getselcfg\.asp</code>\)\, with an optional <code>limit</code> on the number of entries returned\. Documents that the same log is also readable over plain IPMI via <code>pyghmi</code>\'s <code>get\_event\_log\(\)</code> \(confirmed against the target hardware\, same 24 entries\) and that IPMI is generally the better choice\; this module exists for web\-management\-only reachability and cross\-checking\, not to duplicate that path\. Strictly read\-only \-\- no <code>clear</code> option exists or is planned on this module\, and the paged <code>getselentries\.asp</code> \(POST\) endpoint is deliberately not used\, since <code>AspClient\.get\_webvar\(\)</code> is GET\-only by design\.
* Add <code>asmb8\_sessions</code>\, a read\-only module reporting per\-service session capacity and remote\-session \(KVM/media\) configuration \(<code>getallservicescfg\.asp</code>\, <code>getremotesession\.asp</code>\)\. Session\-count fields are decoded from this board\'s \+128 offset encoding\, confirmed against two independent capacity measurements already recorded elsewhere in this collection\. A live directory of active sessions \(<code>getsessioninfo\.asp</code>\) is not implemented\, because that endpoint\'s real invocation requires <code>POST</code> and <code>AspClient\.get\_webvar</code> is deliberately <code>GET</code>\-only\; this is a documented gap pending a POST\-capable client method\, not an oversight\.
* Add <code>asmb8\_users</code>\, a read\-only module reporting local BMC user accounts\, the current session\'s privilege role\, and LDAP/AD role\-group bindings \(<code>getalluserinfo\.asp</code>\, <code>getrole\.asp</code>\, <code>getallrolegroupcfg\.asp</code>\)\. Empty user\-table slots are reported as slots\, not accounts\; e\-mail addresses and SSH key material are never returned\, only booleans indicating whether they are configured\; unsourced numeric privilege\-limit fields are returned raw with an explicit caveat rather than decoded\. No write capability is implemented \-\- account creation\, modification\, and role\-group changes are deliberately out of scope\, since a mistake there can lock an operator out of the BMC entirely\.
* Add <code>docs/asmb8\_console\.md</code>\, matching the documentation page every other original module already has\, and correct its own prior draft\'s claim that no unit/mock test exists for <code>ivtp\.py</code> now that one does \-\- while being explicit that this remains Tier 2 \(mock\) evidence only \-\- every IVTP wire fact is still sourced from a decompiled vendor client\, never from a live capture beyond the bare 8\-byte greeting\, and no console frame has ever been decoded from real hardware\.
* Add <code>plugins/module\_utils/webvar\.py</code>\, a parser for AMI MegaRAC\'s <code>\.asp</code> WEBVAR/JSONVAR response format \(the JavaScript\-object\-literal shape returned by endpoints such as <code>getdatetime\.asp</code>\, <code>getallsensors\.asp</code> and <code>getfwinfo\.asp</code>\)\, backed by a 54\-file corpus of real\, redacted responses captured from a live ASMB8\-iKVM BMC \(<code>tests/unit/fixtures/asp/</code>\)\. This is shared foundation for planned read\-only reporting modules and does not itself change any existing module\'s behavior\.
* Add <code>tests/integration/mock\_servers/ivtp\_server\.py</code>\, a deterministic mock IVTP KVM/console endpoint \(the <code>asmb8\_console</code> counterpart to <code>IusbMockServer</code>\)\, and drive <code>plugins/module\_utils/ivtp\.py</code>\'s real <code>open\_channel</code>/<code>capture\_one\_frame</code> against it over loopback sockets in <code>tests/unit/mock\_servers/test\_ivtp\_server\.py</code> \-\- a full successful handshake\, fragmented video\-frame reassembly\, a rejected session token\, an unsolicited <code>STOP\_SESSION\_IMMEDIATE</code> both mid\-handshake and mid\-frame\, a truncated header\, a truncated body\, and a <code>pktSize</code> that disagrees with the bytes actually sent\. This closes the coverage gap <code>docs/capability\-matrix\.md</code> previously named explicitly \-\- <code>asmb8\_console</code>/<code>ivtp\.py</code> had zero unit/mock test coverage of the handshake state machine\, the only module and <code>module\_utils</code> file in this collection with neither\. Also adds a stronger\, real\-server\-backed regression for <code>capture\=decoded\_frame</code>\'s existing network\-untouched\-before\-refusal behavior in <code>tests/unit/plugins/modules/test\_asmb8\_console\.py</code>\.
* asmb8\_alerts \- add a new\, read\-only module that reads the ASMB8\-iKVM BMC\'s alerting configuration \(SMTP relay\, LAN alert destinations\, the IPMI PEF table\, alert policies\, email formatting\, and per\-policy last\-triggered timestamps\) over the C\(\.asp\) web\-management surface\, grouped by what a caller wants to know rather than by endpoint name\. Implements no writes \-\- see the module\'s own documentation for why changing alert destinations is deliberately out of scope for this release\.
* asmb8\_auditlog \- add a new\, read\-only module that reads the ASMB8\-iKVM BMC\'s audit log entries and its logging configuration \(remote/SD\-card mirroring\, syslog enablement and rotation\, and the IPMI SEL overflow policy\) over the C\(\.asp\) web\-management surface\. Entries are returned verbatim and are documented as potentially containing usernames or addresses\. Implements no writes \-\- clearing the audit log is deliberately out of scope for this release\.
* asmb8\_inventory \- new read\-only module for <code>getfwinfo\.asp</code>\, <code>getprojectcfg\.asp</code>\, and <code>getfruinfo\.asp</code>\. Decodes <code>getfwinfo\.asp</code>\'s <code>FirmwareRevision2</code> as BCD \(<code>20</code> decimal is <code>0x14</code> hex\, i\.e\. \"14\"\, not \"20\"\) so <code>firmware\.firmware\_version</code> renders as <code>1\.14</code> \-\- matching this board\'s own \"firmware 1\.14\, aux 1\.14\.2\" as independently recorded in <code>docs/protocol\-notes\.md</code> and <code>README\.md</code> \-\- and never as the wrong <code>1\.20</code>\. Combines the three <code>MfgID\_\*</code> bytes into a little\-endian manufacturer id without asserting which organisation it belongs to\, since no sourced lookup for that value exists\. Reports <code>getprojectcfg\.asp</code>\'s full 42\-entry <code>FEATURES</code> list verbatim \(including its two real duplicates\) alongside a deduplicated <code>feature\_set</code>\, correcting an assumption in this module\'s original task brief that the fixture only carries one feature\. Reports <code>getfruinfo\.asp</code> honestly as unpopulated on the target board \(one of the corpus\'s five sentinel\-only fixtures\) rather than inventing a normalized shape with no populated sample to source it from\. Added to <code>meta/runtime\.yml</code>\'s <code>asmb8\_ikvm</code> action group\.
* asmb8\_sensors \- new read\-only module for <code>getallsensors\.asp</code> \(48 real sensor records on the target board\)\. Scales <code>SensorReading</code> by the empirically\-derived factor of 1000 into <code>reading\.value</code> \(cross\-checked against nominal rail voltages\, a plausible CPU temperature\, and a plausible fan speed in the fixture\) rather than presenting <code>RawReading</code> directly \-\- the voltage sensors in particular prove <code>RawReading</code> alone is not a usable value\. Splits sensors into <code>threshold</code> \(a real analog reading\) versus <code>discrete</code> \(event/state\-only\, where <code>SensorReading</code> has been observed to carry a placeholder rather than a real value\) using each record\'s own <code>SettableReadableFlags</code> field\. Decodes <code>sensor\_type</code>/reading unit from the standard IPMI specification\'s Sensor Type/Unit Type Codes tables\, leaving the vendor\-OEM sensor type range unmapped rather than inventing a name for it\. Supports filtering by sensor name/type and always groups the result by decoded type name\. Documents how this differs from reading sensors generically over IPMI via <code>pyghmi</code> \(already a dependency of this collection\) and when to prefer each\. Added to <code>meta/runtime\.yml</code>\'s <code>asmb8\_ikvm</code> action group\.
* roles/asmb8\_baremetal\_install \- document that interrupting a play \(not only a hard C\(kill \-9\)\) can strand the BMC\'s virtual\-media slot on firmware that predates this release\'s <code>SIGTERM</code> fix\, what the symptom looks like \(an C\(ESTABLISHED\) TCP connection to port 5120 with unread bytes and zero SCSI commands serviced\, and no C\(vmedia\: redirection accepted\) log line\)\, and that O\(james\_crowley\.asmb8\_ikvm\.asmb8\_reset\) is the recovery path when software reclamation cannot see the session holding it\.

<a id="bugfixes"></a>
### Bugfixes

* asmb8\_media \- fix a defect where an uncleanly\-terminated background media\-session daemon \(a <code>SIGTERM</code>\, e\.g\. from an interrupted play or a stray <code>pkill</code>\, previously arriving with no guaranteed effect\) could leave the BMC\'s single\, board\-wide C\(cd\-media\) slot held forever \-\- C\(getallservicescfg\.asp\) confirms this BMC applies no server\-side timeout at all \(C\(SERVICE\_TIMEOUT\: 4294967295\)\) to reclaim it\. The background daemon\'s existing <code>SIGTERM</code> handler is now proven\, by a test that forks a real daemon process and sends it a real <code>SIGTERM</code>\, to route through the exact same normal\-exit teardown a local O\(state\=detached\) call already used \-\- closing the iUSB session \(which sends the TCP C\(FIN\) the BMC needs to free the slot\) before the process exits \-\- rather than merely having a handler registered\. Deliberately not <code>SIGINT</code>\: a backgrounded process inherits C\(SIG\_IGN\) for that signal from its launching shell\'s own job control\, so C\(kill \-INT\) on one is silently swallowed\. The signal handler itself only ever sets a flag \(never touches the network\, a lock\, or any other non\-signal\-safe state\)\, which also makes it naturally idempotent \-\- a second <code>SIGTERM</code> arriving mid\-shutdown is a no\-op\, not a hang or an exception\.
* asmb8\_media \- the background daemon\'s state file\, and RV\(operation\.observed\.stop\_reason\)\, now record WHY a O\(state\=detached\) session actually stopped \(V\(signal\)\, V\(peer\_closed\)\, or V\(bmc\_terminate\)\)\, mirroring O\(james\_crowley\.asmb8\_ikvm\.asmb8\_http\_origin\)\'s identically\-named field\, so a post\-mortem can tell a signalled stop apart from a BMC\-initiated one\.

<a id="new-modules"></a>
### New Modules

* james\_crowley\.asmb8\_ikvm\.asmb8\_alerts \- Read the ASMB8\-iKVM BMC\'s alerting configuration \(SMTP\, PEF\, policies\, LAN destinations\)
* james\_crowley\.asmb8\_ikvm\.asmb8\_auditlog \- Read the ASMB8\-iKVM BMC\'s audit log and its logging configuration
* james\_crowley\.asmb8\_ikvm\.asmb8\_identify \- Control the ASMB8\-iKVM chassis identify LED over standard IPMI
* james\_crowley\.asmb8\_ikvm\.asmb8\_inventory \- Read ASMB8\-iKVM firmware\, FRU\, and project\-feature inventory over <code>\.asp</code>
* james\_crowley\.asmb8\_ikvm\.asmb8\_sensors \- Read ASMB8\-iKVM sensor readings over the <code>\.asp</code> web\-management surface

<a id="v0-3-0"></a>
## v0\.3\.0

<a id="release-summary-1"></a>
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

<a id="minor-changes-1"></a>
### Minor Changes

* <code>asmb8\_media</code> \- add a regression test pinning that the KVM/media token is resolved from the JNLP by flag name rather than by argument position\. The shipped parser was already correct\; the test exists because a diagnostic harness read the token positionally as the fourth <code>\<argument\></code>\, which on this firmware is <code>\-hostname</code>\'s value \-\- the BMC\'s own IP address\. Authenticating with that made the BMC refuse redirection with an otherwise\-undocumented status <code>3</code>\, a failure whose only visible symptom was an established\-but\-idle socket\. Status <code>3</code> means \"bad token\"\. The test uses this firmware\'s real argument order so a future refactor back to index arithmetic fails in CI rather than on hardware\.
* asmb8\_http\_origin \- new module\, an ephemeral local HTTP file server for handing installer files to a target that only speaks HTTP rather than this collection\'s native iUSB path\. Kept to this collection\'s \"no standing infrastructure\" rule with a hard\, unconditional <code>lifetime\_seconds</code> cap so a background instance self\-terminates even if the play that started it crashes or the controller loses power mid\-run\; per\-request path confinement against <code>\.\.</code>\, symlink\, and percent\-encoding \(including double\-encoding\) traversal\; a structured per\-request access log for diagnosing a failed install\; and correct <code>Range</code>/<code>206 Partial Content</code> handling for bootloaders and installers that issue ranged reads\. Added to <code>meta/runtime\.yml</code>\'s <code>asmb8\_ikvm</code> action group alongside the other six modules\.
* asmb8\_media \- the background media session\'s state file now records idle\-streak bookkeeping \(<code>idle\_polls</code>\, <code>current\_idle\_streak</code>\, <code>last\_idle\_streak</code>\, <code>idle\_poll\_interval\_seconds</code>\) so a post\-mortem can separate a healthy\, expected idle period from a connection that actually broke\, instead of relying on a single point\-in\-time read of <code>updated\_at</code>\. Surfaced under <code>operation\.observed</code> on every <code>asmb8\_media</code> call\.
* asmb8\_reset \- new module for BMC cold/warm self\-reset over IPMI \(netfn <code>0x06</code> cmd <code>0x02</code>/<code>0x03</code>\)\, promoting the documented manual <code>ipmitool mc reset cold</code> recovery step to a first\-class\, testable module\. Destructive\-adjacent\: drops every active BMC session\, including any in\-flight virtual\-media session\, though the host itself is unaffected \(verified live\: the host stayed powered on and never rebooted across a real cold reset\)\. Supports full <code>check\_mode</code>\, which never opens an IPMI connection at all \-\- a self\-reset has no idempotent \"previous state\" to read\. Added to <code>meta/runtime\.yml</code>\'s <code>asmb8\_ikvm</code> action group\.

<a id="bugfixes-1"></a>
### Bugfixes

* asmb8\_baremetal\_install \- raise the default <code>asmb8\_baremetal\_install\_handoff\_timeout</code> from <code>3600</code> to <code>7200</code> seconds\. A real\, unattended Proxmox install against the target hardware was killed by the old one\-hour default at 70\% complete\; see the role\'s README \(\"Expected duration\"\) for the sizing arithmetic and how to compute a value for your own ISO\.

<a id="new-modules-1"></a>
### New Modules

* james\_crowley\.asmb8\_ikvm\.asmb8\_http\_origin \- Run \(or stop\) an ephemeral\, lifetime\-capped local HTTP file server
* james\_crowley\.asmb8\_ikvm\.asmb8\_reset \- Reset the ASMB8\-iKVM BMC\'s management controller over IPMI

<a id="v0-2-0"></a>
## v0\.2\.0

<a id="release-summary-2"></a>
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

<a id="minor-changes-2"></a>
### Minor Changes

* asmb8\_console \- new module\, carrying asmb8\_redirection\'s former IVTP console/KVM\-session implementation \(handshake\, <code>capture\=handshake\_only\|raw\_frame\|decoded\_frame</code>\) unchanged in behaviour\. Added to <code>meta/runtime\.yml</code>\'s <code>asmb8\_ikvm</code> action group alongside the other five modules\.

<a id="bugfixes-2"></a>
### Bugfixes

* <code>asmb8\_media</code> \- disable Nagle\'s algorithm \(<code>TCP\_NODELAY</code>\) on the iUSB media socket\, which Python\'s <code>socket\.create\_connection</code> leaves enabled by default\. iUSB is strictly synchronous request/response\, the traffic shape Nagle handles worst\: each reply is written in a single <code>sendall</code>\, but its final partial segment was previously withheld pending an ACK the peer delays\. This is the theoretically\-correct default for this traffic shape regardless of measured impact\. A controlled A/B test on real hardware \(same install\, same ISO\, same machine\, with and without the option\) found no measurable throughput difference \- the two runs matched within about 2\% throughout and were byte\-identical from roughly the 3\.5\-minute mark onward\. Recorded as a negative result\: this option was not the install\'s latency bottleneck on this hardware\, and should not be re\-investigated for that reason without new evidence\. See <code>docs/hardware\-evidence\-2026\-08\-08\.md</code>\.
* <code>asmb8\_media</code> \- the emulated CD\-ROM\'s <code>READ TOC</code> handler ignored the SCSI CDB\'s allocation length and always returned its full 20\-byte response\. A Linux initrd requesting 12 bytes received 20\, which is a protocol violation\; the host\'s optical layer concluded the disc had no valid track structure\, never read the ISO9660 superblock\, and reported no valid installation medium\. Only a real operating system is affected \- bootloaders read via firmware I/O and never issue <code>READ TOC</code>\, so booting an installer appeared to work while the subsequent handoff to the OS always failed\. The handler now truncates to the allocation length while still reporting the full available length in the TOC data\-length field\, so an initiator that under\-allocated can retry with a larger buffer\.

<a id="v0-1-0"></a>
## v0\.1\.0

<a id="release-summary-3"></a>
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

<a id="new-modules-2"></a>
### New Modules

* james\_crowley\.asmb8\_ikvm\.asmb8\_boot \- Select a one\-time IPMI boot device on an ASMB8\-iKVM endpoint
* james\_crowley\.asmb8\_ikvm\.asmb8\_info \- Gather ASMB8\-iKVM capability and state facts
* james\_crowley\.asmb8\_ikvm\.asmb8\_media \- Attach or detach a local ISO to an ASMB8\-iKVM virtual CD\-ROM over iUSB
* james\_crowley\.asmb8\_ikvm\.asmb8\_power \- Control and query ASMB8\-iKVM power state over IPMI
* james\_crowley\.asmb8\_ikvm\.asmb8\_redirection \- Open an ASMB8\-iKVM console/KVM \(IVTP\) session headlessly
