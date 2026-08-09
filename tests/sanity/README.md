<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Sanity test ignores

**There are currently no ignore files in this directory, and no sanity rule this
collection suppresses.** This file records the conventions for adding one, and the
practice that keeps them from being needed — the same policy the sibling
`james_crowley.intel_amt` collection follows. This is deliberate policy, not
neglect: an empty directory here does not mean sanity has not run yet, it means
every sanity finding so far has been fixed rather than suppressed.

## If you ever need an ignore

One `ignore-<ansible-core-version>.txt` per version the ignore actually applies
to. Entries must be a single line with an optional *trailing* comment; a standalone
comment line fails the `ignores` sanity test itself. An ignore for a rule that does
not fire on that version is also reported as unnecessary, so scope each file to the
versions that genuinely flag the code.

Keep the list short. Every entry is a rule we have chosen not to enforce, so each
needs a reason that survives review.

## Run sanity against the oldest supported ansible-core, not just the newest

Sanity behaviour genuinely differs between versions. When changing anything a
sanity test inspects, run at least the oldest supported version locally too,
not only whatever you happen to have installed:

```bash
python3 -m venv /tmp/v217 && /tmp/v217/bin/pip install "ansible-core~=2.17.0"
COLLECTION_PATH="$(./scripts/setup-collection-tree.sh)"
cd "$COLLECTION_PATH" && /tmp/v217/bin/ansible-test sanity --venv
```
