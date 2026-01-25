FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Create non-root user for security
RUN useradd -m -u 1000 telegram && \
    mkdir -p /data/backups && \
    chown -R telegram:telegram /app /data && \
    chmod +x /app/scripts/entrypoint.sh

# Switch to non-root user
USER telegram

# Set default environment variables
ENV BACKUP_PATH=/data/backups \
    LOG_LEVEL=INFO \
    PYTHONPATH=/app

# Volume for persistent data
VOLUME ["/data"]

# Entrypoint runs migrations, then hands off to CMD
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

# Run scheduler
CMD ["python", "-m", "src.scheduler"]
