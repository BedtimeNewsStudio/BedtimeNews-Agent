FROM python:3.14-slim

# curl: the compose healthcheck (curl -fsS http://localhost:8000/health) and debugging
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Run the API as a dedicated unprivileged account.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home \
    --shell /usr/sbin/nologin app

# Set working directory
WORKDIR /app

# Copy project metadata and install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy Python scripts
COPY src/ ./src/

# Create log directory
RUN mkdir -p /var/log/agent \
    && chown app:app /var/log/agent

# Keep logs immediate and the application filesystem read-only at runtime.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

# Run the agentic RAG retrieval API.
# `exec` replaces the shell, so uvicorn is PID 1 and receives Docker's SIGTERM
# directly (a plain `sh -c` would swallow it, and the grace period would end in
# SIGKILL). On SIGTERM uvicorn stops accepting connections and lets in-flight
# requests, including streaming /chat, finish for up to 270 s: longer than the
# 240 s /chat limit (src/chat.py), shorter than compose's 300 s
# stop_grace_period (compose.app.yml).
CMD ["sh", "-c", "exec python -m uvicorn src.main:app --host 0.0.0.0 --port ${AGENT_BACKEND_PORT:-8000} --timeout-graceful-shutdown 270"]
