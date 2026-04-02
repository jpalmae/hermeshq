FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir git+https://github.com/NousResearch/hermes-agent.git

COPY backend/hermeshq /app/hermeshq

RUN mkdir -p /app/workspaces

EXPOSE 8000

CMD ["uvicorn", "hermeshq.main:app", "--host", "0.0.0.0", "--port", "8000"]

