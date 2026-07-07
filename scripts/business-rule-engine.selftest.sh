#!/usr/bin/env bash
# =============================================================================
# business-rule-engine.selftest.sh — offline self-test wrapper for the Business
# Rule Engine's pure functions (cosmic_common bundle).
#
# Runs docker/langflow-extensions/cosmic_common/components/cosmic_common/business_rule_engine_selftest.py
# (stdlib-only, no network, no Docker, no LangFlow/LangGraph) which asserts the
# engine's pure _evaluate_rules: the four calculation rule kinds (sum, pct_of,
# amount, formula), the restricted recursive-descent formula parser (no `/`, no
# eval), Kahn topological sort (cycle / duplicate / unknown output →
# AR_VALIDATION), assert rules (ops + evaluated after calcs), strict vs
# non-strict, malformed-rule handling, rate resolution (literal | path | $GV),
# and the seed ruleset end-to-end. Stubs lfx so it runs on the host without the
# in-image venv. Picked up by `make test` and CI alongside the other
# *.selftest.sh suites.
#
# Run with: ./scripts/business-rule-engine.selftest.sh   (also via `make test`)
# =============================================================================
set -euo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SELFTEST_DIR}/.." && pwd)"

python3 "${REPO_ROOT}/docker/langflow-extensions/cosmic_common/components/cosmic_common/business_rule_engine_selftest.py"