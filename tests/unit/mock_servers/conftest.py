# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Makes ``tests/integration/mock_servers`` importable from these tests.

The mock servers are standalone scripts (deliberately not part of the
``ansible_collections`` plugin tree -- they are never shipped in the built
collection artifact), so they need their own directory on ``sys.path`` rather
than being importable via the collection's normal namespace-package
machinery. Mirrors the sibling ``james_crowley.intel_amt`` collection's
``tests/unit/mock_servers/conftest.py`` exactly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_MOCK_SERVERS_DIR = Path(__file__).resolve().parents[2] / "integration" / "mock_servers"
if str(_MOCK_SERVERS_DIR) not in sys.path:
    sys.path.insert(0, str(_MOCK_SERVERS_DIR))
