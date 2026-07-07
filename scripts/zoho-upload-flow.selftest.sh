#!/usr/bin/env bash
# =============================================================================
# zoho-upload-flow.selftest.sh — offline self-test wrapper for the Cosmic AR
# Zoho Upload Flow (ar_issue_invoice, the 7th AR subflow — ADR-0011).
#
# Runs docker/langflow-extensions/ar_common/components/ar_common/zoho_upload_flow_selftest.py
# (stdlib-only, no network, no Docker, no LangFlow/LangGraph) which asserts the
# Zoho Upload Flow's pure functions + end-to-end graph: ZohoUploadRequest wrapper
# parsing (missing/bad approval_ref → AR_FORBIDDEN §1; missing/empty invoices /
# malformed → AR_VALIDATION), InvoiceData mandatory-field validation,
# deterministic idempotency_key (ar-idem:invoice_issue:<tenant>:<uuid5>), §10
# retry over a stub transport (success/duplicate/transient-then-success/hard-4xx/
# auth-401/all-transient-exhausted), all-or-nothing rollback of created invoices
# on a partial batch, canonical ZohoUploadResult (operation invoice_issue,
# additionalProperties:false), AuditRecord per create/rollback (§13),
# WorkflowState (posted_total = Σ non-rolled-back, status completed/failed), §11
# checkpoints, §14 envelope shape, and run() end-to-end (8 scenarios incl.
# single/batch/partial-rollback/all-failed/duplicate/forbidden/validation/malformed).
# Stubs lfx + langgraph so it runs on the host without the in-image venv. Picked
# up by `make test` and CI alongside the other *.selftest.sh suites.
#
# Run with: ./scripts/zoho-upload-flow.selftest.sh   (also via `make test`)
# =============================================================================
set -euo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SELFTEST_DIR}/.." && pwd)"

python3 "${REPO_ROOT}/docker/langflow-extensions/ar_common/components/ar_common/zoho_upload_flow_selftest.py"