#!/usr/bin/env bash
# Fail if .circleci/config.yml contains a double angle bracket that is not a
# CircleCI parameter tag.
#
# Why this exists: CircleCI's preprocessor treats a double angle bracket as the
# start of a parameter tag, anywhere in the file, including inside a shell
# `command:` block and inside comments. A bash here-string therefore produces
# "Unclosed tag", which is a CONFIG error -- the affected workflow fails to
# launch entirely.
#
# `circleci config validate` does NOT catch this. It reports such a config as
# valid, and the breakage only surfaces the next time the affected workflow is
# actually triggered. In this collection the hardware workflow is reachable only
# via a pipeline parameter that defaults to false, so the gap between "merged"
# and "found out" could be several releases. This guard closes that gap in the
# lint job, which runs on every commit.
#
# Inherited from the sibling james_crowley.intel_amt collection, where the
# original incident occurred.
set -euo pipefail

config="${1:-.circleci/config.yml}"

if [ ! -f "${config}" ]; then
    echo "ERROR: ${config} not found" >&2
    exit 1
fi

total=$(grep -oE '<<' "${config}" | wc -l | tr -d ' ')
tags=$(grep -oE '<< *(pipeline\.)?parameters\.' "${config}" | wc -l | tr -d ' ')

if [ "${total}" -ne "${tags}" ]; then
    echo "ERROR: ${config} contains $((total - tags)) double-angle-bracket sequence(s)" >&2
    echo "that are not CircleCI parameter tags. CircleCI will fail to parse the" >&2
    echo "config with \"Unclosed tag\" and the workflow will not launch." >&2
    echo >&2
    echo "Offending lines:" >&2
    grep -nE '<<' "${config}" | grep -vE '<< *(pipeline\.)?parameters\.' >&2
    echo >&2
    echo "Rewrite without the sequence -- including in comments. For splitting a" >&2
    echo "string in bash, set IFS around a bare 'for' instead of a here-string." >&2
    exit 1
fi

echo "OK: ${config} has ${tags} parameter tag(s) and no stray double angle brackets."
