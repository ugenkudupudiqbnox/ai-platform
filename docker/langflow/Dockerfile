# =============================================================================
# LangFlow — production image.
# Wraps the upstream LangFlow image with a dispatching entrypoint that runs
# either the gunicorn web tier ("web") or a Celery worker ("worker").
# =============================================================================
ARG LANGFLOW_IMAGE=langflowai/langflow:1.1.1

FROM ${LANGFLOW_IMAGE}

# Bring in our entrypoints and gunicorn configuration (world-readable/executable
# so the image's existing non-root user can run them).
COPY --chmod=0755 entrypoint.sh /opt/aiplatform/entrypoint.sh
COPY --chmod=0755 worker-entrypoint.sh /opt/aiplatform/worker-entrypoint.sh
COPY --chmod=0644 gunicorn.conf.py /opt/aiplatform/gunicorn.conf.py

ENV LANGFLOW_CONFIG_DIR=/var/lib/langflow \
    GUNICORN_CONF=/opt/aiplatform/gunicorn.conf.py \
    PYTHONUNBUFFERED=1

EXPOSE 7860 9090

ENTRYPOINT ["/opt/aiplatform/entrypoint.sh"]
CMD ["web"]
