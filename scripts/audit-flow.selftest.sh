#!/usr/bin/env bash
# =============================================================================
# audit-flow.selftest.sh — offline self-test wrapper for the Cosmic AR Audit
# Flow (ar_audit, the 16th AR subflow — ADR-0012).
#
# Runs docker/langflow-extensions/ar_common/components/ar_common/audit_flow_selftest.py
# (stdlib-only, no network, no Docker, no LangFlow/LangGraph) which asserts the
# Audit Flow's pure functions + end-to-end graph: AuditRequest wrapper parsing
# (empty/non-object/malformed → AR_VALIDATION), request validation
# (list-field-not-a-list → AR_VALIDATION; bad execution_time → AR_VALIDATION;
# all-lists-empty valid), _collect (summary counts, subflows_invoked from
# execution_history, totals from the last calculation_result), AuditRecord
# synthesis per action type (file.intake/validation.report/calculation.result/
# invoice.generated/approval.decision/invoice.issue/audit.summary — append_only,
# source_system only on zoho/foodics, scalar-only before/after per state_delta),
# _build_audit_log (Σ artifacts + 1 summary), ExecutionSummary (intent ar_audit,
# totals, subflows_invoked, approvals, checkpoint_id, additionalProperties:false),
# WorkflowState (completed, pending_approvals [], idempotency_keys {},
# additionalProperties:false), §11 checkpoints, §14 envelope shape, and run()
# end-to-end (full bundle / empty bundle / malformed / list-not-a-list /
# source_system handling). Stubs lfx + langgraph so it runs on the host without
# the in-image venv. Picked up by `make test` and CI alongside the other
# *.selftest.sh suites.
#
# Run with: ./scripts/audit-flow.selftest.sh   (also via `make test`)
# =============================================================================
set -euo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SELFTEST_DIR}/.." && pwd)"

python3 "${REPO_ROOT}/docker/langflow-extensions/ar_common/components/ar_common/audit_flow_selftest.py"