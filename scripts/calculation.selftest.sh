#!/usr/bin/env bash
# =============================================================================
# calculation.selftest.sh — offline self-test wrapper for the Cosmic AR
# Calculation Flow (ar_calculation, the 14th AR subflow — ADR-0008).
#
# Runs docker/langflow-extensions/ar_common/components/ar_common/calculation_selftest.py
# (stdlib-only, no network, no Docker, no LangFlow/LangGraph) which asserts the
# Calculation Flow's pure functions + end-to-end graph: validated-JSON payload
# parsing, parameter resolution (missing rates default to 0.00 + warning),
# payload validation + exception classification, CalculationResult assembly
# (calculation_type="reconcile", the 9 signed-2dp totals, line_items with
# source_refs=[rule_id]), WorkflowState snapshot, per-calculation checkpoints
# (§11), §14 envelope shape, and run() end-to-end (good payload → AR_OK + 9
# figures + 3 checkpoints; malformed JSON / no facts → AR_VALIDATION; missing
# rate → warning + 0.00). Stubs lfx + langgraph so it runs on the host without
# the in-image venv. Picked up by `make test` and CI alongside the other
# *.selftest.sh suites.
#
# Run with: ./scripts/calculation.selftest.sh   (also via `make test`)
# =============================================================================
set -euo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SELFTEST_DIR}/.." && pwd)"

python3 "${REPO_ROOT}/docker/langflow-extensions/ar_common/components/ar_common/calculation_selftest.py"