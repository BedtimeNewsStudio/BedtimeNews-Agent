FROM python:3.14-slim

# Install system dependencies
RUN apt-get update && apt-get install -y git \
    && rm -rf /var/lib/apt/lists/*

# Run indexing and scheduling as a dedicated unprivileged account.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home \
    --shell /usr/sbin/nologin app

# Set working directory
WORKDIR /app

# Copy project metadata and install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy source code and configuration files
COPY src/ ./src/
COPY index_config.yml index_config.sample.yml ./

# The bind-mounted archive and persistent scheduler logs are the only writable
# runtime locations.
RUN mkdir -p /data /var/log/indexer \
    && chown app:app /data /var/log/indexer

# Keep logs immediate and the application filesystem read-only at runtime.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

# Start scheduled indexing with one immediate pipeline execution.
ENTRYPOINT ["python", "-m", "src.entrypoint", "--run-immediately"]
