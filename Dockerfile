FROM python:3.12-slim

RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*

# Create non-root user and group
RUN groupadd -g 999 python && \
    useradd -r -u 999 -g python python

WORKDIR /app

# Copy and install dependencies
COPY satoricheck/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files and set correct permissions
COPY satoricheck/ .
RUN chown -R python:python /app

# Switch to non-root user
USER python

ENV PORT=8080
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 backend.server:app

