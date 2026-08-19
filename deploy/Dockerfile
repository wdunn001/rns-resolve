FROM python:3.12-slim

RUN pip install --no-cache-dir rns psycopg2-binary

WORKDIR /app
COPY rns_resolve /app/rns_resolve
COPY deploy/rns/config /app/default-rns-config

ENV RESOLVE_RNS_CONFIG=/config \
    RESOLVE_DB=/data/resolve.db

CMD ["sh", "-c", "mkdir -p /config /data && [ -f /config/config ] || cp /app/default-rns-config /config/config; exec python -m rns_resolve.service"]
