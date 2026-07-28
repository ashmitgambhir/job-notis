# Two stages: build the wheel with a toolchain, run it without one.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# Install into a venv we can copy wholesale into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# EXTRAS controls image size: the default install has no torch. Build with
#   --build-arg EXTRAS="[embeddings]"
# for local semantic matching (adds roughly 2GB).
ARG EXTRAS=""
RUN pip install --upgrade pip setuptools wheel && pip install ".${EXTRAS}"


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    JOBMON_CONFIG_DIR=/app/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 jobmon

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY config ./config
COPY --chown=jobmon:jobmon . /app

# Persist the database and logs across restarts.
RUN mkdir -p /app/data/logs && chown -R jobmon:jobmon /app/data
VOLUME ["/app/data"]

USER jobmon
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

# tini reaps zombies and forwards SIGTERM, so the daemon shuts down cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["jobmon", "run"]
