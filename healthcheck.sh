#!/usr/bin/env bash
# =============================================================================
# healthcheck.sh — operational health snapshot of the platform.
# Reports container health, host resources and per-service metrics.
# Exit code is non-zero if any required container is not healthy/running.
#
#   ./healthcheck.sh
# =============================================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${SCRIPT_DIR}/scripts/common.sh"

[ -f "${ENV_FILE}" ] || die ".env not found — run ./install.sh first."

RC=0

# Services with a container healthcheck (or whose running state we require).
# langfuse-worker and oauth2-proxy have no HTTP healthcheck; langflow-worker is
# optional (disabled on LangFlow versions without a Celery app), so they are not
# treated as required-healthy here.
REQUIRED_SERVICES=(postgres redis mongo clickhouse minio keycloak \
  langfuse-web langflow flower librechat nginx)

heading "Container status"
for svc in "${REQUIRED_SERVICES[@]}"; do
  cid="$(dc ps -q "${svc}" 2>/dev/null || true)"
  if [ -z "${cid}" ]; then
    error "${svc}: not running"
    RC=1
    continue
  fi
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${cid}" 2>/dev/null || echo unknown)"
  case "${status}" in
    healthy|running) success "${svc}: ${status}" ;;
    *) error "${svc}: ${status}"; RC=1 ;;
  esac
done

heading "Host resources"
echo "Disk usage (filesystem holding Docker data):"
df -h / 2>/dev/null | awk 'NR==1 || /\/$/'
echo
echo "Memory:"
free -h 2>/dev/null || warn "free not available"
echo
echo "Load average:$(awk '{printf " %s %s %s", $1,$2,$3}' /proc/loadavg 2>/dev/null)"

heading "Container resource usage"
# Word splitting on the container-id list is intentional here.
# shellcheck disable=SC2046
docker stats --no-stream --format \
  "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}" \
  $(dc ps -q 2>/dev/null) 2>/dev/null || warn "Could not read docker stats."

heading "Redis metrics"
RPASS="$(get_env REDIS_PASSWORD)"
if redis_out="$(dc exec -T redis redis-cli -a "${RPASS}" --no-auth-warning INFO 2>/dev/null)"; then
  echo "${redis_out}" | grep -E '^(connected_clients|used_memory_human|maxmemory_human|mem_fragmentation_ratio|evicted_keys|keyspace_hits|keyspace_misses|uptime_in_days):' \
    | sed 's/\r//' | sed 's/^/  /'
  echo "  --- queue depth (LangFlow Celery broker, db 1) ---"
  broker_db="$(get_env REDIS_DB_LANGFLOW_BROKER)"
  qlen="$(dc exec -T redis redis-cli -a "${RPASS}" --no-auth-warning -n "${broker_db}" LLEN celery 2>/dev/null | tr -d '\r')"
  echo "  celery queue length: ${qlen:-0}"
else
  warn "Could not query Redis."
fi

heading "PostgreSQL metrics"
PGUSER="$(get_env POSTGRES_SUPER_USER)"
PGPASS="$(get_env POSTGRES_SUPER_PASSWORD)"
if pg_out="$(dc exec -T -e PGPASSWORD="${PGPASS}" postgres psql -U "${PGUSER}" -d postgres -tA -F '|' -c \
    "SELECT datname, numbackends, xact_commit, xact_rollback FROM pg_stat_database WHERE datname IN ('keycloak','langflow','langfuse','LibreChat','postgres');" 2>/dev/null)"; then
  echo "  database | connections | commits | rollbacks"
  # shellcheck disable=SC2001
  echo "${pg_out}" | sed 's/^/  /'
else
  warn "Could not query PostgreSQL."
fi

heading "LangFlow worker / queue"
worker_count="$(dc ps --format '{{.Service}}' 2>/dev/null | grep -c '^langflow-worker$' || true)"
echo "  langflow-worker replicas running: ${worker_count:-0}"

heading "NGINX"
if dc exec -T nginx nginx -t >/dev/null 2>&1; then
  success "nginx configuration is valid"
else
  error "nginx configuration test failed"; RC=1
fi

echo
if [ "${RC}" -eq 0 ]; then
  success "All required services are healthy."
else
  error "One or more services are unhealthy (see above)."
fi
exit "${RC}"
