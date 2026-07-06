#!/usr/bin/env bash
# =============================================================================
# PostgreSQL bootstrap — Cosmic AR Agent checkpoint database.
# Runs once on first initialization of the data volume, after 01-databases.sh.
#
# Creates the least-privilege role + database used by the LangGraph checkpointer
# (constitution §11, architecture §11) to persist AgentState in ar_checkpoints.
#
# This is a SCAFFOLD placeholder. It is INERT until the build phase wires the
# AR_AGENT_DB_* env vars onto the `postgres` service in docker-compose.yml and
# generates AR_AGENT_DB_PASSWORD in scripts/gen-secrets.sh (see
# cosmic-ar/README.md#build-phase-platform-integration). When any of those vars
# is unset/empty, the script prints a skip notice and exits 0 so a real first
# init of an existing deployment is unaffected.
# =============================================================================
set -euo pipefail

# Guard: do nothing until the build phase wires the env onto the postgres service.
if [ -z "${AR_AGENT_DB_NAME:-}" ] || [ -z "${AR_AGENT_DB_USER:-}" ] || [ -z "${AR_AGENT_DB_PASSWORD:-}" ]; then
  echo "==> Skipping ar_agent database: AR_AGENT_DB_* not configured (build-phase wiring pending)."
  exit 0
fi

echo "==> Provisioning database '${AR_AGENT_DB_NAME}' owned by role '${AR_AGENT_DB_USER}'"

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-SQL
  DO \$\$
  BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '${AR_AGENT_DB_USER}') THEN
      CREATE ROLE "${AR_AGENT_DB_USER}" LOGIN PASSWORD '${AR_AGENT_DB_PASSWORD}';
    ELSE
      ALTER ROLE "${AR_AGENT_DB_USER}" WITH LOGIN PASSWORD '${AR_AGENT_DB_PASSWORD}';
    END IF;
  END
  \$\$;
SQL

# CREATE DATABASE cannot run inside a transaction/DO block; guard with a check.
if ! psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${AR_AGENT_DB_NAME}'" \
      --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" | grep -q 1; then
  psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" \
    -c "CREATE DATABASE \"${AR_AGENT_DB_NAME}\" OWNER \"${AR_AGENT_DB_USER}\";"
fi

# Revoke the implicit PUBLIC grants and hand exclusive control to the owner.
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-SQL
  REVOKE ALL ON DATABASE "${AR_AGENT_DB_NAME}" FROM PUBLIC;
  GRANT ALL PRIVILEGES ON DATABASE "${AR_AGENT_DB_NAME}" TO "${AR_AGENT_DB_USER}";
SQL

# Lock down the public schema inside the new database to the owner only.
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${AR_AGENT_DB_NAME}" <<-SQL
  REVOKE ALL ON SCHEMA public FROM PUBLIC;
  GRANT ALL ON SCHEMA public TO "${AR_AGENT_DB_USER}";
  ALTER SCHEMA public OWNER TO "${AR_AGENT_DB_USER}";
SQL

echo "==> ar_agent database provisioned successfully."