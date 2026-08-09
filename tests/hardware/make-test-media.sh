#!/usr/bin/env bash
#
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Provision the one small local test ISO tests/hardware/media_attach.yml needs,
# entirely inside the workspace. Never committed -- .gitignore already blocks
# *.iso/*.img -- so every environment that runs that playbook (the lab runner,
# or a developer's own machine) must materialise it itself.
#
# Adapted from the sibling james_crowley.intel_amt collection's script of the
# same name and reasoning: it fetches iPXE's own ipxe.iso (a few MB). It is
# genuinely bootable (a real network bootloader, not a blank filler) and
# small. Unlike that collection, this board has exactly one virtual-media
# slot -- a read-only CD-ROM -- so there is no second, writable image to
# provision here; see README.md's "Virtual media" section on why this board's
# CD-ROM channel has no write opcode at all.
#
# This is a network request to boot.ipxe.org, never to the BMC under test --
# it makes no request of any kind to real hardware.
#
# Idempotent and safe to re-run: an existing file is kept if it is non-empty.
# Pass --force to always refetch.
#
# Usage:
#   ./tests/hardware/make-test-media.sh [--force] [output-path]
#
# Respects ASMB8_TEST_ISO_PATH if already exported, so the CI step and this
# script always agree on where the file lives. Falls back to
# tests/hardware/output/media/asmb8-test.iso next to this script otherwise.

set -euo pipefail

FORCE=false
if [ "${1:-}" = "--force" ]; then
    FORCE=true
    shift
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEDIA_DIR="${SCRIPT_DIR}/output/media"
mkdir -p "${MEDIA_DIR}"

ISO_PATH="${1:-${ASMB8_TEST_ISO_PATH:-${MEDIA_DIR}/asmb8-test.iso}}"

# iPXE publishes stable, versioned build artifacts at boot.ipxe.org. https first;
# http as a fallback for lab networks that only proxy plain HTTP outbound.
IPXE_ISO_URLS=(
    "https://boot.ipxe.org/ipxe.iso"
    "http://boot.ipxe.org/ipxe.iso"
)

file_size() {
    wc -c <"$1" | tr -d ' '
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

need_iso=true
if [ "${FORCE}" = false ] && [ -s "${ISO_PATH}" ]; then
    echo "Reusing existing test ISO at ${ISO_PATH} ($(file_size "${ISO_PATH}") bytes)."
    need_iso=false
fi

if [ "${need_iso}" = true ]; then
    mkdir -p "$(dirname "${ISO_PATH}")"
    tmp_iso="$(mktemp "${MEDIA_DIR}/.asmb8-test-download.XXXXXX")"
    fetched=false
    for url in "${IPXE_ISO_URLS[@]}"; do
        echo "Fetching bootable test ISO from ${url} ..."
        if curl -fsSL --retry 3 --retry-connrefused --connect-timeout 10 -o "${tmp_iso}" "${url}"; then
            fetched=true
            break
        fi
        echo "  ... failed, trying next URL if any remain." >&2
    done

    if [ "${fetched}" = false ]; then
        rm -f "${tmp_iso}"
        echo "ERROR: could not fetch a bootable test ISO from any of: ${IPXE_ISO_URLS[*]}" >&2
        echo "Refusing to produce a placeholder file -- media_attach.yml needs media" >&2
        echo "genuinely small enough and readable, and a silent empty/junk file would" >&2
        echo "make a green run meaningless." >&2
        exit 1
    fi

    downloaded_size="$(file_size "${tmp_iso}")"
    if [ "${downloaded_size}" -eq 0 ]; then
        rm -f "${tmp_iso}"
        echo "ERROR: downloaded ISO is empty (0 bytes). Refusing to use it." >&2
        exit 1
    fi

    mv "${tmp_iso}" "${ISO_PATH}"
    echo "Fetched test ISO: ${downloaded_size} bytes."
fi

echo
echo "Test media ready:"
echo "  ${ISO_PATH}"
echo "    size:   $(file_size "${ISO_PATH}") bytes"
echo "    sha256: $(sha256_of "${ISO_PATH}")"
echo
echo "ASMB8_TEST_ISO_PATH=${ISO_PATH}"
