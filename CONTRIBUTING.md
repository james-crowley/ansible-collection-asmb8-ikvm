<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Contributing

Practical instructions for working on this collection, plus the traps that
have already cost real time on its sibling, `james_crowley.intel_amt`, and
that apply here too because they are properties of `ansible-core` and this
project's own tooling, not of Intel AMT specifically.

This collection is pre-1.0 and not hardware-qualified (see
[`docs/capability-matrix.md`](docs/capability-matrix.md)), but it is not
scaffolding: six modules, two roles, a full mock-integration tier, and a
`.circleci/config.yml` all exist and are exercised on every push. What
remains genuinely unbuilt is narrower than that might suggest — see
[`docs/testing.md`](docs/testing.md) for exactly which tier covers what.

## Before you start: stage the collection tree

`ansible-test` insists the collection live at
`<root>/ansible_collections/james_crowley/asmb8_ikvm/`, regardless of where
you actually checked the repository out. `scripts/setup-collection-tree.sh`
materialises that layout and prints the resulting path:

```bash
COLLECTION_PATH="$(./scripts/setup-collection-tree.sh)"
cd "$COLLECTION_PATH"
```

Two things about this that will cost you time if you miss them:

1. **It copies, not symlinks.** `ansible-test` resolves paths in ways that
   make a symlinked collection root behave inconsistently across
   `ansible-core` versions, so this script does a real copy instead. The
   direct consequence: **the staged tree goes stale the moment you edit a
   source file again.** Re-run the script after every edit, or every
   subsequent `ansible-test`/`pytest` invocation is testing a snapshot that
   no longer matches your working tree.
2. **Set `COLLECTION_BUILD_ROOT` when more than one checkout might stage
   concurrently** (parallel CI jobs, several worktrees, more than one agent
   or contributor building on the same host at once). Left unset, the
   script derives a build root from a checksum of the repository's own
   path — which means two *different* checkouts at the *same* path can
   collide on the same build root and clobber each other mid-run:

   ```bash
   export COLLECTION_BUILD_ROOT=/tmp/my-build-root
   COLLECTION_PATH="$(./scripts/setup-collection-tree.sh)"
   ```

## Install the tooling

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install "ansible-core~=2.19.0" -r requirements.txt -r requirements-dev.txt
```

`requirements-dev.txt` pins exact versions on purpose — see its own comment
for why. Bump the pins in their own PR and fix whatever the new version
finds there.

`ansible-core~=2.19.0` above matches what `.circleci/config.yml`'s `lint`
job installs; the collection's declared floor is `>=2.17.0`
(`meta/runtime.yml`), and anything in that range works locally too — see
`docs/testing.md`'s breakdown of exactly which `ansible-core`/Python cells
CI actually covers.

## Local verification sequence

This is what `.circleci/config.yml`'s `lint`/`sanity`/`units`/
`integration-mock` jobs run, in the same order, so a green local run and a
green CI run are the same claim:

```bash
# From the repository root:
export COLLECTION_BUILD_ROOT=/tmp/my-build-root   # see above
COLLECTION_PATH="$(./scripts/setup-collection-tree.sh)"
cd "$COLLECTION_PATH"

ansible-test sanity --venv --python 3.12
ansible-test units  --venv --python 3.12
ansible-test integration --venv --python 3.12   # against local mock .asp/iUSB servers

# Back at the repository root:
cd -
yamllint -c .yamllint .
ruff check plugins tests
ruff format --check plugins tests

# ansible-lint resolves `james_crowley.asmb8_ikvm.*` FQCNs by looking in the
# collections path, and unlike ansible-test it does not stage the tree itself.
mkdir -p ~/.ansible/collections/ansible_collections/james_crowley
ln -sfn "$(pwd)" ~/.ansible/collections/ansible_collections/james_crowley/asmb8_ikvm
ansible-lint --offline

ansible-galaxy collection build --output-path /tmp/dbuild --force
```

If you touched `.circleci/config.yml` itself, also run its own guard before
anything else, exactly as the `lint` job does — a stray `<<...>>` sequence
anywhere in that file, including inside a comment, silently breaks parsing:

```bash
./scripts/check-circleci-tags.sh .circleci/config.yml
```

### Running `pytest` directly, for a fast inner loop

`ansible-test units` builds a fresh virtualenv every invocation, which is the
right thing for CI and far too slow when you are iterating on one test. You
can run `pytest` yourself, but **not from the repository root** — a test
that imports its subject as
`ansible_collections.james_crowley.asmb8_ikvm.plugins.…` only resolves if
the collection is sitting inside an `ansible_collections/` directory that is
itself on `sys.path`. From the root you get:

```
ModuleNotFoundError: No module named 'ansible_collections'
```

That is not a missing dependency and installing something will not fix it.
It needs the staged tree, run from inside it, with `PYTHONPATH` pointing at
the tree *root* (the directory containing `ansible_collections/`, i.e. two
levels above the collection):

```bash
export COLLECTION_BUILD_ROOT=/tmp/my-build-root
COLLECTION_PATH="$(./scripts/setup-collection-tree.sh)"
cd "$COLLECTION_PATH"
PYTHONPATH="${COLLECTION_BUILD_ROOT}" pytest tests/unit -q
```

Two reminders that follow from the staging being a **copy**: re-run
`setup-collection-tree.sh` after every source edit, and remember you are
editing files in the repository while running files under
`${TMPDIR:-/tmp}` — a "my fix had no effect" moment here is almost always a
stale tree.

## Practical traps

These are not theoretical for this collection's tooling — each one has
already produced a real, confusing failure on `james_crowley.intel_amt`,
which shares this repository's `ansible-core` floor, doc-string
conventions, and lint config, or on this collection itself.

### `ansible-core >= 2.17` only, and there is no dual-compatible sanity boilerplate

The sanity-test boilerplate requirement changed **incompatibly** at
`ansible-core` 2.17. Every module and `module_utils` file needs:

```python
from __future__ import annotations
```

at the top (after the license header, before any other import). The older
`from __future__ import absolute_import, division, print_function` plus a
module-level `__metaclass__ = type` pair — which is what pre-2.17 sanity
wants — is now **rejected** by 2.17+'s sanity checks. There is no single
form that passes sanity on both an old and a new `ansible-core`. This is why
the collection's floor is `ansible-core >= 2.17`, full stop.

### Any doc string containing a colon-space must be a block scalar

If a `DOCUMENTATION`/`RETURN`/doc-fragment YAML string contains a literal
`: ` (colon followed by a space) — for example `C(delegate_to: localhost)`
— it **must** be written as a YAML block scalar (`>-` or `|-`), never a
plain scalar. In a plain scalar, YAML reads the embedded colon-space as a
mapping key/value separator and the `yamllint` sanity test fails.
`plugins/doc_fragments/connection.py` and every module's `DOCUMENTATION`
already do this correctly — watch for it in anything new, including a
hardware playbook's `name:` field (a real, fixed instance of exactly this
trap: see `tests/hardware/boot_once.yml`'s git history).

### `ansible-core >= 2.21` needs `_ANSIBLE_PROFILE = "legacy"` in unit tests

Unit tests that drive `AnsibleModule` directly by setting
`basic._ANSIBLE_ARGS` (every `tests/unit/plugins/modules/test_*.py`'s
`_set_module_args()` helper) must also set:

```python
basic._ANSIBLE_PROFILE = "legacy"
```

`ansible-core >= 2.21` requires this explicit args-decoding profile
alongside `_ANSIBLE_ARGS`; older cores simply ignore the attribute, so
setting it unconditionally is harmless across the whole supported range.
Forgetting it only breaks on the newest `ansible-core` in the test matrix,
which makes it easy to miss locally if you are not pinned to that version.

### Never add an inline `# noqa` for a rule `pyproject.toml` already ignores per-file

`ruff`'s `RUF100` (unused `noqa` directive) is enabled, so a `# noqa: X`
whose finding was already suppressed at config level is itself an error.
Check `[tool.ruff.lint.per-file-ignores]` before reaching for an inline
comment — in particular `E402` in `plugins/modules/*.py`, ignored
project-wide because Ansible's module convention requires the
`DOCUMENTATION`/`EXAMPLES`/`RETURN` string literals at the top of the file,
before any import.

### `ansible-playbook --syntax-check` catches YAML colon-in-scalar bugs `yamllint` does not

`tests/hardware/*.yml`'s task `name:` fields are plain, unquoted scalars.
`Read the boot-device override before this run (check mode: never mutates)`
as a task name is invalid YAML (the `: ` inside the parentheses reads as a
mapping separator) and `ansible-playbook` refuses to parse the file — but
`yamllint` alone does not catch it, because the file is still syntactically
valid YAML for a shorter, differently-shaped mapping. Run
`ansible-playbook --syntax-check` on any new or edited playbook, not just
`yamllint`, before opening a PR.

## Conventional commits and changelog fragments

Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, …). Keep commits
atomic — one logical change per commit, with a real body explaining *why*,
not just *what*.

**Every user-facing change needs a changelog fragment** in
`changelogs/fragments/`, following the `antsibull-changelog` format
configured in `changelogs/config.yaml`. Name it after your change
(`changelogs/fragments/<something-descriptive>.yml`) — the filename is not
load-bearing, only the contents are. `changelogs/config.yaml` sets
`keep_fragments: false`, so `antsibull-changelog release` **deletes** every
fragment once it has folded it into `CHANGELOG.md`/`CHANGELOG.rst`. The
shape:

```yaml
---
bugfixes:
  - "asmb8_media - correctly report the iUSB session as closed after a timeout ... (https://github.com/james-crowley/ansible-collection-asmb8-ikvm/issues/1)."
```

Valid top-level sections (from `changelogs/config.yaml`): `major_changes`,
`minor_changes`, `breaking_changes`, `deprecated_features`,
`removed_features`, `security_fixes`, `bugfixes`, `known_issues`. Use
Ansible doc markup (`` C() ``, `` O() ``, `` V() ``, `` M() ``) inside
fragment entries, exactly as in module `DOCUMENTATION`/`RETURN` strings, and
link back to the issue or PR the change addresses. Purely internal changes
(test-only refactors, CI tweaks with no user-visible effect) do not need a
fragment, but if in doubt, add one.

## Scope reminders

- There is deliberately **no committed `tests/hardware/inventory.yml`** the
  way the sibling `james_crowley.intel_amt` collection has one — this
  collection targets one board and reads its address/credentials from
  `tests/hardware/vars/connection.yml`, which is nothing but
  `lookup('ansible.builtin.env', ...)` expressions. If you add a second
  board and that file grows a real per-host inventory, keep every real
  hostname, credential, and certificate fingerprint out of it — see
  `tests/hardware/README.md`.
- Never commit real boot media (`*.iso`, `*.img` are gitignored except under
  test fixture directories). `tests/hardware/make-test-media.sh` provisions
  a disposable test ISO on demand instead.
- If you touch `docs/protocol-notes.md`, treat the byte layouts and field
  mappings there as normative — "improving" them without a firmware capture
  or reference source to back the change is exactly the kind of unverified
  drift this project cannot afford, given that the iUSB protocol has no
  public specification to fall back on.
- If you touch anything under `tests/hardware/`, re-read that directory's own
  `README.md` and `PREFLIGHT.md` first: those playbooks are written for
  someone about to touch real hardware, and a change that looks like a
  harmless refactor there can silently widen what an approval actually
  authorises.
