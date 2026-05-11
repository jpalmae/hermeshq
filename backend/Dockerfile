# ============================================================
# Stage 1 – builder: compile everything heavy
# ============================================================
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

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
RUN pip install --no-cache-dir --prefix=/install -r /app/requirements.txt \
    && pip install --no-cache-dir --prefix=/install \
       git+https://github.com/NousResearch/hermes-agent.git@v2026.5.7

# --- Node.js dependencies (whatsapp-bridge) ---
RUN mkdir -p /app/hermeshq/assets/whatsapp-bridge
COPY backend/hermeshq/assets/whatsapp-bridge/package.json  /app/hermeshq/assets/whatsapp-bridge/package.json
COPY backend/hermeshq/assets/whatsapp-bridge/package-lock.json /app/hermeshq/assets/whatsapp-bridge/package-lock.json
RUN cd /app/hermeshq/assets/whatsapp-bridge \
    && npm ci --omit=peer --no-audit --no-fund

# ============================================================
# Stage 2 – runtime: slim image, non-root user
# ============================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    psmisc \
    curl \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /install /usr/local

# Copy node_modules from builder
COPY --from=builder /app/hermeshq/assets/whatsapp-bridge/node_modules \
     /app/hermeshq/assets/whatsapp-bridge/node_modules

# Copy application source
COPY VERSION /app/VERSION
COPY backend/hermeshq /app/hermeshq

# Writable directories for appuser
RUN mkdir -p /app/workspaces \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "hermeshq.main:app", "--host", "0.0.0.0", "--port", "8000"]
