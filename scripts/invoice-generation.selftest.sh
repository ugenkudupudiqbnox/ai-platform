#!/usr/bin/env bash
# =============================================================================
# invoice-generation.selftest.sh — offline self-test wrapper for the Cosmic AR
# Invoice Generation Flow (ar_invoice_generation, the 15th AR subflow — ADR-0009).
#
# Runs docker/langflow-extensions/ar_common/components/ar_common/invoice_generation_selftest.py
# (stdlib-only, no network, no Docker, no LangFlow/LangGraph) which asserts the
# Invoice Generation Flow's pure functions + end-to-end graph: validated-JSON
# invoice-request parsing, payload validation + exception classification,
# InvoiceData assembly (deterministic uuid5 ids shaped IG-<customer>-<8hex>,
# status="draft", due_date = issue + 30, 2dp amounts, _validate_invoice guard),
# the 8 artifacts (Invoice JSON / PDF render-spec / Excel render-spec / draft
# Journal Entry / Customer Statement / Zoho Upload File / Invoice Metadata /
# WorkflowState), per-generation checkpoints (§11 — 8 labels), §14 envelope
# shape, and run() end-to-end (good payload → AR_OK + 8 artifacts + 8
# checkpoints; malformed JSON / no line_items / no customer_ref → AR_VALIDATION;
# missing optional fields → still AR_OK). Stubs lfx + langgraph so it runs on the
# host without the in-image venv. Picked up by `make test` and CI alongside the
# other *.selftest.sh suites.
#
# Run with: ./scripts/invoice-generation.selftest.sh   (also via `make test`)
# =============================================================================
set -euo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SELFTEST_DIR}/.." && pwd)"

python3 "${REPO_ROOT}/docker/langflow-extensions/ar_common/components/ar_common/invoice_generation_selftest.py"