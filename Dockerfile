FROM python:3.12-slim

RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY satoricheck/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY satoricheck/ .

ENV PORT=8080
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 backend.server:app
