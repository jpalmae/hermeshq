# ============================================================
# Stage 1 – builder: compile everything heavy
# ============================================================
FROM docker:29.2.1-cli AS docker-cli

FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python dependencies ---
COPY backend/requirements.txt /app/requirements.txt
COPY backend/hermes-agent-cryptography.patch /app/hermes-agent-cryptography.patch
# hermes-agent is pinned by commit SHA (tags are mutable). v2026.8.3 = 3c27eb
RUN python -m venv /opt/venv \
    && pip install --no-cache-dir \
       "setuptools>=84,<85" "wheel>=0.48,<0.49" \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && git init -q /opt/hermes-agent \
    && git -C /opt/hermes-agent remote add origin https://github.com/NousResearch/hermes-agent.git \
    && git -C /opt/hermes-agent fetch -q --depth 1 origin 3c27eb6234bf91b8ceee9e9071591b31e9b148cb \
    && git -C /opt/hermes-agent checkout -q --detach FETCH_HEAD \
    && git -C /opt/hermes-agent apply /app/hermes-agent-cryptography.patch \
    && pip install --no-cache-dir -e /opt/hermes-agent \
    && pip install --no-cache-dir --upgrade -r /app/requirements.txt

# --- Node.js dependencies (whatsapp-bridge) ---
RUN mkdir -p /app/hermeshq/assets/whatsapp-bridge
COPY backend/hermeshq/assets/whatsapp-bridge/package.json  /app/hermeshq/assets/whatsapp-bridge/package.json
COPY backend/hermeshq/assets/whatsapp-bridge/package-lock.json /app/hermeshq/assets/whatsapp-bridge/package-lock.json
RUN cd /app/hermeshq/assets/whatsapp-bridge \
    && npm ci --omit=peer --no-audit --no-fund

RUN mkdir -p /app/pi-runtime
COPY backend/hermeshq/assets/pi-runtime/package.json /app/pi-runtime/package.json
COPY backend/hermeshq/assets/pi-runtime/package-lock.json /app/pi-runtime/package-lock.json
RUN cd /app/pi-runtime && npm ci --omit=dev --no-audit --no-fund

# ============================================================
# Stage 2 – runtime: slim image, non-root user
# ============================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

# Runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    psmisc \
    git \
    curl \
    gnupg \
    libglib2.0-0 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libfreetype6 \
    libjpeg62-turbo \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/python -m pip install --no-cache-dir \
       "setuptools>=84,<85" "wheel>=0.46.2,<0.47"

# Create non-root user
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hermes-agent /opt/hermes-agent
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# Copy node_modules from builder
COPY --from=builder /app/hermeshq/assets/whatsapp-bridge/node_modules \
     /app/hermeshq/assets/whatsapp-bridge/node_modules
COPY --from=builder /app/pi-runtime/node_modules /app/node_modules

# Copy application source
COPY VERSION /app/VERSION
COPY backend/alembic.ini /app/alembic.ini
COPY backend/hermeshq /app/hermeshq

# Writable directories for appuser
RUN mkdir -p /app/workspaces \
    && chown -R appuser:appuser /app

ENV WORKSPACES_ROOT=/app/workspaces

USER appuser

EXPOSE 8000

CMD ["uvicorn", "hermeshq.main:app", "--host", "0.0.0.0", "--port", "8000"]
