#!/usr/bin/env sh
# Install / start the TraceX offline appliance on a Linux host with Docker.
# Works without network access when images.tar (from the offline bundle) is present.
set -eu
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker Engine with the compose plugin is required (https://docs.docker.com/engine/install/)." >&2
  exit 1
fi
if [ -f images.tar ]; then
  echo "Loading images from images.tar (offline install)..."
  docker load -i images.tar
fi
if [ ! -f .env ]; then
  cp .env.example .env
  secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  dbpass=$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')
  sed -i "s/^TRACEX_SECRET_KEY=.*/TRACEX_SECRET_KEY=${secret}/; s/^TRACEX_DB_PASSWORD=.*/TRACEX_DB_PASSWORD=${dbpass}/" .env
  chmod 600 .env
  echo "Created .env with fresh secrets."
fi
docker compose up -d
port=$(grep -E '^TRACEX_PORT=' .env | cut -d= -f2)
bind=$(grep -E '^TRACEX_BIND=' .env | cut -d= -f2)
echo "Waiting for TraceX..."
for _ in $(seq 1 90); do
  if docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/healthz', timeout=3)" >/dev/null 2>&1; then
    echo "TraceX is up: http://${bind:-127.0.0.1}:${port:-8000}/"
    exit 0
  fi
  sleep 2
done
echo "TraceX did not become healthy; see: docker compose logs api worker" >&2
exit 1
