#!/usr/bin/env bash
# scripts/intercompany-sales.selftest.sh — offline self-test wrapper for the
# Intercompany Sales Flow's pure functions (constitution §1/§4/§8/§9/§10/§11/
# §14/§15/§16). Run by `make test` and CI. Exits non-zero on any failure.
#
# The actual test logic lives in the stdlib-only Python module next to the
# component; this wrapper just locates the repo root (so it runs from anywhere)
# and invokes it. Mirrors scripts/file-intake.selftest.sh.
set -euo pipefail

SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SELFTEST_DIR}/.." && pwd)"

exec python3 \
  "${REPO_ROOT}/docker/langflow-extensions/ar_common/components/ar_common/intercompany_sales_selftest.py"