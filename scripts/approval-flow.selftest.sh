#!/usr/bin/env bash
# =============================================================================
# approval-flow.selftest.sh — offline self-test wrapper for the Cosmic AR
# Human Approval Flow (ar_approval, the 9th AR subflow — ADR-0010).
#
# Runs docker/langflow-extensions/ar_common/components/ar_common/approval_flow_selftest.py
# (stdlib-only, no network, no Docker, no LangFlow/LangGraph) which asserts the
# Human Approval Flow's pure functions + end-to-end pause/resume graph:
# review-packet parsing (good/empty/malformed/non-object/missing-action/
# missing-proposal → AR_VALIDATION), ApprovalRequest assembly (approval_ref
# shaped ar-approval-<uuid>, tier packet>override>default, requested_by
# fallbacks, 2dp amount), presentation packet (4 summaries + 3 options),
# decision normalization + reply parsing (approve/reject/request changes +
# synonyms → canonical; no-verb → None), ApprovalResult (consumed=false),
# AuditRecord (append_only=true, actor=decided_by, before/after delta),
# WorkflowState (status=completed, totals 0.00, pending_approvals=[]), per-gate
# checkpoints (§11: packet/decision/state/audit/ar_approval), §14 envelope
# shape, and run() end-to-end pause/resume (good packet → pending_approval +
# ref + 3 options; resume approve/reject/request_changes → AR_OK + 1 audit
# record; resume garbage → AR_FORBIDDEN; malformed JSON → AR_VALIDATION; never
# raises). The custom walker models interrupt() pause/resume (the base walker
# stubs interrupt → None). Stubs lfx + langgraph so it runs on the host without
# the in-image venv. Picked up by `make test` and CI alongside the other
# *.selftest.sh suites.
#
# Run with: ./scripts/approval-flow.selftest.sh   (also via `make test`)
# =============================================================================
set -euo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SELFTEST_DIR}/.." && pwd)"

python3 "${REPO_ROOT}/docker/langflow-extensions/ar_common/components/ar_common/approval_flow_selftest.py"