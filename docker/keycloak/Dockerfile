# =============================================================================
# Keycloak — optimized production build.
# Pre-builds the server for the Postgres database vendor so that the runtime can
# start with `--optimized` (no build step at container start).
# =============================================================================
ARG KEYCLOAK_IMAGE=quay.io/keycloak/keycloak:26.0

FROM ${KEYCLOAK_IMAGE} AS builder

ENV KC_DB=postgres
ENV KC_HEALTH_ENABLED=true
ENV KC_METRICS_ENABLED=true
ENV KC_FEATURES=token-exchange,admin-fine-grained-authz

# Build the optimized server image.
RUN /opt/keycloak/bin/kc.sh build

FROM ${KEYCLOAK_IMAGE}

COPY --from=builder /opt/keycloak/ /opt/keycloak/

# Custom branded login theme.
COPY themes/ /opt/keycloak/themes/

ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
