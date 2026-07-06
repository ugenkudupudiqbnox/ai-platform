#!/usr/bin/env bash
# =============================================================================
# adapter.selftest.sh — offline self-test wrapper for the OpenAI adapter.
#
# Runs docker/langflow-adapter/adapter_selftest.py (stdlib-only, no network,
# no Docker) which asserts the adapter's pure functions: file extraction from
# OpenAI/LibreChat messages, data-URL decoding, §14 envelope parsing, approval-
# ref detection, and pending-approval rendering. Picked up by `make test` and
# CI alongside the other *.selftest.sh suites.
#
# Run with: ./scripts/adapter.selftest.sh   (also via `make test`)
# =============================================================================
set -euo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SELFTEST_DIR}/.." && pwd)"

python3 "${REPO_ROOT}/docker/langflow-adapter/adapter_selftest.py"