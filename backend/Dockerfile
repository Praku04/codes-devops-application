FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY backend /app

ENV PYTHONPATH=/app

# Port is configurable via API_PORT (docker-compose passes it from .env). The shell form
# of CMD is required so $API_PORT is expanded at run time — the exec/JSON form would pass
# the literal string to uvicorn.
ENV API_PORT=8555
EXPOSE ${API_PORT}

CMD uvicorn app.main:app --host 0.0.0.0 --port ${API_PORT}
