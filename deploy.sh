#!/bin/bash
set -euo pipefail

echo "=== Deploying ConceptDigger ==="

# ensure shared ss-network exists using exact inspect check
if ! docker network inspect ss-network >/dev/null 2>&1; then
  docker network create ss-network
fi

# ensure letsencrypt directory and acme.json exist with strict permissions
mkdir -p letsencrypt
touch letsencrypt/acme.json
chmod 600 letsencrypt/acme.json

# build and update containers seamlessly without pre-tearing down
docker compose -f docker-compose.yml -f docker-compose-traefik.yml up -d --build --remove-orphans

echo "=== ConceptDigger Deployed Successfully! ==="