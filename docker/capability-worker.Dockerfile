# Generic isolated worker image for Python capability repositories.
#
# Build context must contain both the Odysseus and capability repository:
#   docker build -f odysseus/docker/capability-worker.Dockerfile \
#     --build-arg CAPABILITY_PROJECT=pain-miner .
FROM python:3.12-slim

ARG CAPABILITY_PROJECT

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*
RUN test -n "$CAPABILITY_PROJECT"
WORKDIR /capability

COPY ${CAPABILITY_PROJECT}/ /capability/
RUN pip install --no-cache-dir . fastapi uvicorn pyyaml

COPY odysseus/integrations/capabilities/worker.py /opt/odysseus_capability_worker.py
COPY odysseus/docker/capability-worker-entrypoint.sh /usr/local/bin/capability-entrypoint

RUN useradd -u 1000 -m capability \
    && mkdir -p /data/runs \
    && chown -R capability:capability /capability /data \
    && chmod +x /usr/local/bin/capability-entrypoint

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/capability-entrypoint"]
CMD ["uvicorn", "--app-dir", "/opt", "odysseus_capability_worker:app", "--host", "0.0.0.0", "--port", "8080"]
