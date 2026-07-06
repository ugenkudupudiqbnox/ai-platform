#!/usr/bin/env bash
# Offline self-test wrapper for the Cosmic Kitchen Revenue Flow's pure helpers
# (constitution self-test convention; run by `make test` and CI). Exits non-zero
# on any assertion failure. Stdlib-only — no LangFlow, no Docker, no openpyxl.
set -euo pipefail

SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SELFTEST_DIR}/.." && pwd)"

exec python3 \
  "${REPO_ROOT}/docker/langflow-extensions/ar_common/components/ar_common/kitchen_revenue_selftest.py"