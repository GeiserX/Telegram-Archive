# Builder stage: the C toolchain lives here and only here. Every compiled
# dependency in uv.lock ships a cp314 manylinux wheel today, but keeping gcc
# in the builder means a future sdist-only dependency still builds — without
# publishing a 182 MB compiler layer the runtime never invokes.
FROM python:3.14-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast, reproducible dependency installation
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

# Install dependencies (locked versions)
# --locked fails the build if uv.lock is out of date with pyproject.toml,
# instead of silently shipping an image that is missing a declared dependency.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Runtime stage: ffmpeg (video thumbnail extraction) is the only system
# dependency the runtime actually invokes.
FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# The venv moves between stages verbatim: both are the same base image, so
# the interpreter path the venv links against is identical.
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Create non-root user for security
RUN useradd -m -u 1000 telegram && \
    mkdir -p /data/backups && \
    chown -R telegram:telegram /data && \
    chmod +x /app/scripts/entrypoint.sh

# Switch to non-root user
USER telegram

# Set default environment variables
ENV BACKUP_PATH=/data/backups \
    LOG_LEVEL=INFO \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Volume for persistent data
VOLUME ["/data"]

# A dead archiver must not look healthy: the scheduler touches a heartbeat
# file while its event loop is responsive; this check compares its age.
# start-period covers entrypoint migrations + Telegram connect on big archives.
HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 \
  CMD ["python3", "/app/scripts/healthcheck_backup.py"]

# Entrypoint runs migrations, then hands off to CMD
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

# Default: show help (requires explicit command)
CMD ["python", "-m", "src"]
