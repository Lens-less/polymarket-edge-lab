# Polymarket Market Making Bot Dockerfile
# Multi-stage build for optimized production image

# ===== Build Stage =====
FROM python:3.11-slim AS builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies from lock file
COPY requirements.lock /tmp/requirements.lock
RUN pip install --upgrade pip && \
    pip install -r /tmp/requirements.lock

# ===== Production Stage =====
FROM python:3.11-slim AS production

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

# Create non-root user for security
RUN groupadd -r botuser && useradd -r -g botuser botuser

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy application code
COPY src/ ./src/
COPY run_mm.py run_tui.py ./

# Create logs directory with proper permissions
RUN mkdir -p /app/logs && chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# Default command
CMD ["python", "run_mm.py"]

# ===== Development Stage =====
FROM production AS development

# Switch back to root for development tools
USER root

# Install development dependencies
RUN pip install pytest pytest-asyncio pytest-timeout

# Switch back to botuser
USER botuser

# Default command for development (run tests)
CMD ["python", "-m", "pytest", "tests/", "-v"]
