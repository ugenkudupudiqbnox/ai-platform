#!/usr/bin/env bash
# =============================================================================
# file-intake.selftest.sh — offline self-test wrapper for the File Intake Flow.
#
# Runs docker/langflow-extensions/ar_common/components/ar_common/file_intake_selftest.py
# (stdlib-only, no network, no Docker, no openpyxl/pdfplumber) which asserts the
# File Intake Flow's pure functions: report-type detection, file-ref
# normalization, metadata extraction, DocumentManifest assembly + totals
# cross-check, per-document + manifest validation, §14 envelope shape, §10
# retry classification, and the §4 fail-safe (AR_UNCERTAIN). Stubs lfx + langgraph
# so it runs on the host without the in-image venv. Picked up by `make test` and
# CI alongside the other *.selftest.sh suites.
#
# Run with: ./scripts/file-intake.selftest.sh   (also via `make test`)
# =============================================================================
set -euo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SELFTEST_DIR}/.." && pwd)"

python3 "${REPO_ROOT}/docker/langflow-extensions/ar_common/components/ar_common/file_intake_selftest.py"