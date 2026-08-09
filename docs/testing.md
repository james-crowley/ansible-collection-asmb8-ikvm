<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Testing

Three tiers, in increasing cost and risk:

| Tier | Needs | Runs where | Risk |
|---|---|---|---|
| Sanity + unit | nothing | every push | none |
| Mock integration | local mock `.asp`/iUSB servers | every push | none |
| Hardware-in-the-loop | a real ASMB8-iKVM board | self-hosted runner, opt-in | **power-cycles hardware, attaches virtual media, opens a live KVM channel** |

The hardware tier's playbooks (`tests/hardware/*.yml`) now exist — see
[`tests/hardware/README.md`](../tests/hardware/README.md) for the escalation
chain and [`tests/hardware/PREFLIGHT.md`](../tests/hardware/PREFLIGHT.md) for
the two riskiest jobs — but **none of them have ever actually run against
real hardware**. Building the playbooks closes the gap between what
`.circleci/config.yml` names and what is committed; it does not, by itself,
close the gap between "this collection's own understanding of the protocol"
and "confirmed against real firmware". See
[`docs/capability-matrix.md`](capability-matrix.md) Tier 4 for what that means
for this collection's confidence level.

## Stage the collection tree first

`ansible-test` insists the collection live at
`<root>/ansible_collections/james_crowley/asmb8_ikvm/`, regardless of where
the repository is actually checked out. `scripts/setup-collection-tree.sh`
materialises that layout and prints the resulting path:

```bash
COLLECTION_PATH="$(./scripts/setup-collection-tree.sh)"
cd "$COLLECTION_PATH"
```

Two things about this script that will cost you time if you miss them:

1. **It copies, not symlinks** — `ansible-test` resolves paths in ways that
   make a symlinked collection root behave inconsistently across
   `ansible-core` versions. The direct consequence: **the staged tree goes
   stale the moment you edit a source file again.** Re-run the script after
   every edit, or you are testing a snapshot that no longer matches your
   working tree.
2. **Set `COLLECTION_BUILD_ROOT`** whenever more than one checkout might stage
   concurrently (parallel CI jobs, several worktrees, more than one
   contributor or agent on the same host at once). Left unset, the script
   derives a build root from a checksum of the repository's own path, so two
   *different* checkouts at the *same* path collide on the same build root:

   ```bash
   export COLLECTION_BUILD_ROOT=/tmp/my-build-root
   COLLECTION_PATH="$(./scripts/setup-collection-tree.sh)"
   ```

   The script also refuses to `rm -rf` a build root it did not create itself
   (it checks for its own marker file first), and refuses to stage into a
   build root another still-running process of this same script still owns.

## The pytest inner loop

`ansible-test units` builds a fresh virtualenv on every invocation, which is
correct for CI and far too slow for iterating on one test. Run `pytest`
directly instead — but **not from the repository root**: a test that imports
its subject as `ansible_collections.james_crowley.asmb8_ikvm.plugins...` only
resolves once the collection sits inside an `ansible_collections/` directory
that is itself on `sys.path`. From the repository root you get
`ModuleNotFoundError: No module named 'ansible_collections'` — that is not a
missing dependency, and installing something will not fix it.

```bash
export COLLECTION_BUILD_ROOT=/tmp/my-build-root
COLLECTION_PATH="$(./scripts/setup-collection-tree.sh)"
cd "$COLLECTION_PATH"
PYTHONPATH="${COLLECTION_BUILD_ROOT}" pytest tests/unit -q
```

`PYTHONPATH` must point at the directory *containing* `ansible_collections/`
— that is, `COLLECTION_BUILD_ROOT` itself, two levels above the collection
root `pytest tests/unit` is invoked from.

Because the staging step is a **copy**, remember you are running files under
`${TMPDIR:-/tmp}` (or wherever `COLLECTION_BUILD_ROOT` points), not the files
you are editing in the repository. A "my fix had no effect" moment here is
almost always a stale staged tree — re-run `setup-collection-tree.sh`.

`pyproject.toml`'s `[tool.pytest.ini_options]` scopes `testpaths` to
`tests/unit` and enables `--strict-markers`, so an unregistered pytest marker
fails rather than silently passing through.

## `ansible-test sanity` / `units` / `integration`

```bash
COLLECTION_PATH="$(./scripts/setup-collection-tree.sh)"
cd "$COLLECTION_PATH"
ansible-test sanity --venv --python 3.12
ansible-test units  --venv --python 3.12
ansible-test integration --venv --python 3.12
```

CI uses `--venv`, never `--docker`: the CircleCI Docker executor cannot
bind-mount the working directory into a remote-docker container, which
`ansible-test --docker` requires. Reproduce upstream Ansible's own sanity
containers with a `machine` executor (a real VM with a local Docker daemon)
instead, if you specifically need that.

`tests/sanity/README.md` documents this collection's sanity-ignore policy:
**there are currently no ignore files, and none are expected as routine.**
Every sanity finding so far has been fixed, not suppressed — that is a
deliberate practice, not a claim that sanity has never run.

## Lint commands

```bash
# From the repository root, not the staged tree:
yamllint -c .yamllint .
ruff check plugins tests
ruff format --check plugins tests

# ansible-lint resolves james_crowley.asmb8_ikvm.* FQCNs by looking in the
# collections path, and unlike ansible-test it does not stage the tree itself:
mkdir -p ~/.ansible/collections/ansible_collections/james_crowley
ln -sfn "$(pwd)" ~/.ansible/collections/ansible_collections/james_crowley/asmb8_ikvm
ansible-lint --offline
```

`requirements-dev.txt` pins every linter's exact version
(`ansible-lint==26.6.0`, `ruff==0.16.0`, `yamllint==1.38.0`,
`pytest==9.1.1`, `pytest-mock==3.15.1`, `antsibull-changelog==0.35.1`) —
install from that file, not a bare `pip install ruff`, so a lint result is a
function of the code, not of when it happened to run.

## The mock servers and their fault-injection modes

There is exactly one real ASMB8-iKVM board reachable to this project, so
regression coverage beyond the unit tier comes from two deterministic,
standard-library-only mock servers that play the **BMC's side** of each
protocol:

### `AspMockServer` (`tests/integration/mock_servers/asp_server.py`)

Mocks the `.asp` RPC surface and JNLP document. Every default behaviour is
marked in that file's own docstrings as either `VERIFIED LIVE` (reproduces a
directly-captured board behaviour, including ones that look like bugs — a
mock that "fixes" a verified real quirk hides a real interop bug instead of
catching it) or `UNCONFIRMED` (the endpoint path and field name are real, but
the response shape has not been sourced from any capture).

Fault-injection knobs (`AspFaultConfig`):

- `hang_before_response` — the one that matters most. Accepts the TCP
  connection, then never writes a response at all, reproducing this board's
  saturated-worker-pool hang (see
  [`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md)
  and `ErrorClass.BMC_BUSY`). Persistent until a test turns it back off;
  bounded by `hang_seconds` so a test suite does not leak a thread forever.
- `force_login_failure_marker` — one-shot: force the next `create.asp` login
  to answer with a specific `Failure_Login_*` marker regardless of whether
  the supplied credentials were actually correct.

### `IusbMockServer` (`tests/integration/mock_servers/iusb_server.py`)

Mocks the iUSB CD-ROM redirection endpoint and drives a scripted SCSI
conversation *at* a connected client, validating each reply automatically
(sequence-number echo, `dataPacketLen` self-consistency, the offset-25
response-length field, and — when specified — the actual appended data
length). Also marks every default behaviour `VERIFIED LIVE` or `ASSUMED, NOT
VERIFIED`.

Fault-injection knobs (`IusbFaultConfig`):

- `force_auth_status` — one-shot: override the ACK's `connectionStatus` byte
  for the next auth attempt, independent of whether the token matched.
- `truncate_next_frame_to` — one-shot: truncate the next frame this mock
  sends to an exact byte count, for testing a mid-frame stall.
- `lie_next_data_packet_len` — one-shot: declare a `dataPacketLen` in the
  header that does not match the actual payload bytes sent.
- `disconnect_after_next_send` — one-shot: close the connection immediately
  after the next frame, mid-conversation.
- `zero_reserved_bytes` / `sequential_command_counters` — persistent,
  default off: replace the `VERIFIED LIVE` non-zero reserved bytes /
  non-sequential command counter with the "well-behaved" values a naive
  implementation might assume, specifically to prove a client does *not*
  depend on either assumption.

It also models the board's single-session hazard structurally: a second
concurrent connection attempt is answered with an in-use ACK and dropped, and
`hold_slot_without_connection()`/`release_held_slot()` let a test simulate a
*stale* held slot (no real connection at all) to exercise the eject-before-insert
reclamation path in `plugins/module_utils/media_session.py` without needing
two real sockets.

Run the mocks standalone for manual poking via
`tests/integration/mock_servers/run_asp_mock.py` /
`run_iusb_mock.py`.

## `ansible-core >= 2.21` and unit tests

Unit tests that drive `AnsibleModule` directly by setting
`basic._ANSIBLE_ARGS` must also set:

```python
basic._ANSIBLE_PROFILE = "legacy"
```

`ansible-core >= 2.21` requires this explicit args-decoding profile alongside
`_ANSIBLE_ARGS`; older cores simply ignore the attribute, so setting it
unconditionally is harmless across the whole supported range. Every
`tests/unit/plugins/modules/test_*.py`'s `_set_module_args()` helper does this
already — carry it forward in any new one.

## The CircleCI job/workflow layout

`.circleci/config.yml`, version 2.1. Two workflows:

### `test` — ordinary CI, runs on every push

```
lint → sanity (matrix) ┐
     → units  (matrix) ├→ build → publish-approval → publish (tags only)
     → integration-mock┘
```

- **`lint`** — the parameter-tag guard (`scripts/check-circleci-tags.sh`,
  which must run *before* anything else — a stray `<<...>>` sequence anywhere
  in the config file, including inside a shell block or comment, produces an
  "Unclosed tag" failure that silently prevents the affected workflow from
  launching at all, and `circleci config validate` does not catch it), then
  `yamllint`, `ruff check`, `ruff format --check`, and `ansible-lint`.
- **`sanity`** — matrix over `ansible-core` `2.17`, `2.18`, `2.19`, `2.20`,
  `2.21`, all on Python 3.12 (the one interpreter every version in that axis
  supports).
- **`units`** — matrix over Python `3.10`–`3.13` × `ansible-core` `2.17`,
  `2.19`, `2.21`, with excludes forced by upstream support ranges (3.13
  controller support arrived in `ansible-core` 2.18; 2.19 requires Python
  3.11+; 2.21 requires Python 3.12+).
- **`integration-mock`** — runs `ansible-test integration` against the mock
  `.asp`/iUSB servers above. Currently guarded by a skip that exits `0` if
  `tests/integration/targets` holds nothing but `.keep` — this guard must be
  removed in the same change that adds the first real integration target, not
  before and not long after.
- **`build`** — builds the collection artifact and verifies it installs
  cleanly (`ansible-galaxy collection install` + `ansible-doc --list`).
- **`publish`** — gated by a manual approval, restricted to tag pushes on
  `main`, and cross-checks that the git tag matches `galaxy.yml`'s declared
  version before uploading (a Galaxy version is immutable once published, so
  this mismatch must never reach the upload step).

### `hardware` — opt-in only, never runs on an ordinary push

Gated behind the `run-hardware-tests` pipeline parameter (default `false`).
Triggered deliberately:

```bash
curl -X POST https://circleci.com/api/v2/project/<project-slug>/pipeline \
  -u "${CIRCLE_TOKEN}:" -H 'Content-Type: application/json' \
  -d '{"parameters":{"run-hardware-tests":true}}'
```

Runs on a self-hosted CircleCI machine runner (`resource_class:
crowley/asmb8-runner`) inside the lab network, with access to a real
ASMB8-iKVM BMC. It is a strictly linear chain — never a `matrix`, never two
independent jobs sharing a context — specifically because this board's web
server hangs rather than refuses under concurrent load (see [Known
limitations](../README.md#known-limitations)), so nothing about this
workflow's structure may ever let two jobs touch the board at once:

```
lint → hardware-observe
     → [approval] → hardware-login
     → [approval] → hardware-media-attach  (attach + guaranteed detach, when: always)
     → [approval] → hardware-boot-once
     → [approval] → hardware-reset
     → [approval] → hardware-kvm
```

Six independent controls stand in front of any BMC-touching job:

1. `run-hardware-tests`, false by default — an ordinary push cannot reach
   this workflow at all.
2. **A separate manual approval before each escalation** (login,
   media-attach, boot-once, reset, kvm) — one human click never authorises
   more than the one class of action it was clicked for.
   `hardware-observe` itself needs no approval (it is read-only: `.asp` GET
   probes, IPMI `get_power`/`get_bootdev`, a TCP port map — nothing that could
   be rejected or that mutates BMC state), which is what lets an operator
   learn the TLS fingerprint every later job needs to pin, without first being
   approved to do the thing that produces the value the approval protects.
3. The `asmb8-lab` context, restricted to this project only.
4. `branches: only: main`.
5. `verify-hardware-credentials` checks only that `ASMB8_HOST`,
   `ASMB8_USERNAME`, `ASMB8_PASSWORD` are present and that either
   `ASMB8_TLS_FINGERPRINT` is set or `ASMB8_ALLOW_INSECURE=true` is explicit
   — it reports missing variable *names* only, never a value.
6. CircleCI itself refuses to run self-hosted runner jobs for forked-PR
   builds.

`hardware-media-attach`'s detach step runs `when: always` deliberately: the
`cd-media` slot behind port 5120 permits exactly one session with no
server-side timeout to reclaim it, so a crashed or cancelled attach step
would otherwise wedge the board's only media slot for every future job (and
every human) until a physical/BMC reset.

**Every hardware playbook (`tests/hardware/*.yml`) this workflow names now
exists** — see [`tests/hardware/README.md`](../tests/hardware/README.md).
What has not changed is that **none of them have ever been triggered against
real hardware**: `run-hardware-tests` still defaults to `false`, and every
escalation is still behind its own manual approval. See
[`docs/capability-matrix.md`](capability-matrix.md) Tier 4 for exactly what
that does and does not establish.

## Documented traps (see `CONTRIBUTING.md` for the full list)

- **The staged tree goes stale** — re-run `scripts/setup-collection-tree.sh`
  after every source edit; a fix with "no effect" is almost always this.
- **A colon-space (`: `) inside a plain YAML scalar in a
  `DOCUMENTATION`/`RETURN`/doc-fragment string breaks `yamllint`'s sanity
  test.** For example, a description mentioning `C(delegate_to: localhost)`
  must be written as a block scalar (`>-` or `|-`), never a plain scalar —
  YAML reads the embedded colon-space as a mapping separator otherwise. Every
  module's `DOCUMENTATION` and `plugins/doc_fragments/connection.py` already
  do this correctly; watch for it in anything new.
- **`ansible-core >= 2.21` needs `_ANSIBLE_PROFILE = "legacy"`** in any unit
  test that sets `basic._ANSIBLE_ARGS` directly — see above.
- **Never add an inline `# noqa` for a rule `pyproject.toml` already ignores
  per-file.** `ruff`'s `RUF100` treats an already-suppressed `# noqa` as an
  error itself; check `[tool.ruff.lint.per-file-ignores]` first (in
  particular, `E402` is already ignored project-wide for
  `plugins/modules/*.py`, because Ansible's module convention requires
  `DOCUMENTATION`/`EXAMPLES`/`RETURN` string literals before any import).
