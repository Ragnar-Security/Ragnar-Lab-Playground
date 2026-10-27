FROM python:3.12-slim

WORKDIR /app

# Install system dependencies.
# build-essential / libffi-dev support source builds when a platform has no
# gevent or cryptography wheel. Python.h ships with the python:3.12-slim base,
# so no separate python3-dev package is needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt is exported from uv.lock (every package, direct and
# transitive, pinned with hashes; CI fails on drift). --require-hashes makes
# pip refuse anything that is not exactly what the lock pins.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy application files
COPY app/ app/
COPY templates/ templates/
COPY static/ static/
COPY run.py .
COPY scripts/ scripts/
COPY tests/ tests/

# Create directory for data and logs with proper permissions
RUN mkdir -p /app/data && \
    chmod 777 /app/data

# Expose port
EXPOSE 5000

# Run database initialization and start the application
CMD ["sh", "-c", "python scripts/init_db.py && python run.py"]
