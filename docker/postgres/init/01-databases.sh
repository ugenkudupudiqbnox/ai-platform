#!/usr/bin/env bash
# =============================================================================
# PostgreSQL bootstrap — runs once on first initialization of the data volume.
# Creates a least-privilege role + database for each platform service.
#
# The official postgres entrypoint executes every *.sh in
# /docker-entrypoint-initdb.d as the superuser, with PG* env already pointing at
# the local socket. Variables below are injected from docker-compose.yml.
# =============================================================================
set -euo pipefail

# Create a role + database pair if they do not already exist, then lock the
# database down so only its owner has access (least privilege).
create_service_db() {
  local db_name="$1"
  local db_user="$2"
  local db_pass="$3"

  echo "==> Provisioning database '${db_name}' owned by role '${db_user}'"

  psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-SQL
    DO \$\$
    BEGIN
      IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '${db_user}') THEN
        CREATE ROLE "${db_user}" LOGIN PASSWORD '${db_pass}';
      ELSE
        ALTER ROLE "${db_user}" WITH LOGIN PASSWORD '${db_pass}';
      END IF;
    END
    \$\$;
SQL

  # CREATE DATABASE cannot run inside a transaction/DO block; guard with a check.
  if ! psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${db_name}'" \
        --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" | grep -q 1; then
    psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" \
      -c "CREATE DATABASE \"${db_name}\" OWNER \"${db_user}\";"
  fi

  # Revoke the implicit PUBLIC grants and hand exclusive control to the owner.
  psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-SQL
    REVOKE ALL ON DATABASE "${db_name}" FROM PUBLIC;
    GRANT ALL PRIVILEGES ON DATABASE "${db_name}" TO "${db_user}";
SQL

  # Lock down the public schema inside the new database to the owner only.
  psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${db_name}" <<-SQL
    REVOKE ALL ON SCHEMA public FROM PUBLIC;
    GRANT ALL ON SCHEMA public TO "${db_user}";
    ALTER SCHEMA public OWNER TO "${db_user}";
SQL
}

create_service_db "${KEYCLOAK_DB_NAME}" "${KEYCLOAK_DB_USER}" "${KEYCLOAK_DB_PASSWORD}"
create_service_db "${LANGFLOW_DB_NAME}" "${LANGFLOW_DB_USER}" "${LANGFLOW_DB_PASSWORD}"
create_service_db "${LANGFUSE_DB_NAME}" "${LANGFUSE_DB_USER}" "${LANGFUSE_DB_PASSWORD}"

echo "==> PostgreSQL service databases provisioned successfully."
