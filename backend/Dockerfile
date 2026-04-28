FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    ca-certificates \
    gnupg \
    psmisc \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir git+https://github.com/NousResearch/hermes-agent.git

RUN mkdir -p /app/hermeshq/assets/whatsapp-bridge
COPY backend/hermeshq/assets/whatsapp-bridge/package.json /app/hermeshq/assets/whatsapp-bridge/package.json
COPY backend/hermeshq/assets/whatsapp-bridge/package-lock.json /app/hermeshq/assets/whatsapp-bridge/package-lock.json
RUN cd /app/hermeshq/assets/whatsapp-bridge && npm ci --omit=peer --no-audit --no-fund

COPY VERSION /app/VERSION
COPY backend/hermeshq /app/hermeshq

RUN mkdir -p /app/workspaces

EXPOSE 8000

CMD ["uvicorn", "hermeshq.main:app", "--host", "0.0.0.0", "--port", "8000"]
