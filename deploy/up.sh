#!/bin/sh
# One command on any Linux host with Docker: build the sandbox images, start the stack,
# and wait until the app answers. Fails loudly if it does not.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env here: copy .env.example to .env and fill it in" >&2; exit 1; }
docker network inspect cs-egress >/dev/null 2>&1 || docker network create cs-egress >/dev/null
sh sandbox/build.sh
docker compose up -d --build
i=0
until docker compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "the app did not answer /healthz within 2 minutes" >&2
    docker compose logs --tail 60 app >&2
    exit 1
  fi
  sleep 2
done
docker compose ps
echo "Countersign is up."
