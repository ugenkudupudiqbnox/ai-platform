#!/usr/bin/env bash
# =============================================================================
# wait-for.sh — block until a compose service is healthy.
# Usage: wait-for.sh <service> [timeout_seconds]
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

[ $# -ge 1 ] || die "Usage: $0 <service> [timeout_seconds]"
wait_for_service "$1" "${2:-300}"
