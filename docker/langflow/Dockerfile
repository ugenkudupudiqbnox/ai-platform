# =============================================================================
# LangFlow — production image.
# Wraps the upstream LangFlow image with a dispatching entrypoint that runs
# either the gunicorn web tier ("web") or a Celery worker ("worker").
# =============================================================================
ARG LANGFLOW_IMAGE=langflowai/langflow:1.10.1

FROM ${LANGFLOW_IMAGE}

USER root

# Create the persistent config/state dir owned by the image's runtime user
# (uid 1000, group root). A freshly-created named volume mounted here inherits
# this ownership, so LangFlow can write its secret_key / DB without running as
# root.
RUN mkdir -p /var/lib/langflow \
    && chown -R 1000:0 /var/lib/langflow \
    && chmod -R g+rwX /var/lib/langflow

# Bring in our entrypoints (world-readable/executable so the image's existing
# non-root user can run them). The web tier is launched via `langflow run`,
# which serves the UI + API and manages gunicorn/uvicorn workers itself.
COPY --chmod=0755 entrypoint.sh /opt/aiplatform/entrypoint.sh
COPY --chmod=0755 worker-entrypoint.sh /opt/aiplatform/worker-entrypoint.sh

ENV LANGFLOW_CONFIG_DIR=/var/lib/langflow \
    PYTHONUNBUFFERED=1

EXPOSE 7860 9090

# Drop back to the non-root runtime user.
USER 1000

ENTRYPOINT ["/opt/aiplatform/entrypoint.sh"]
CMD ["web"]
