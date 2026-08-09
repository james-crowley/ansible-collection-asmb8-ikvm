# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Mock BMC fixtures for integration testing.

Not part of the ``ansible_collections`` plugin tree, and never shipped in the
built collection artifact (see ``galaxy.yml``'s ``build_ignore``) -- these are
standalone scripts, importable either directly (by ``run_*_mock.py``, or by a
future ``ansible-test integration`` target) or via ``sys.path`` insertion (by
``tests/unit/mock_servers``, which self-tests them). This file exists only so
the directory is an ordinary importable package for the latter case.
"""
