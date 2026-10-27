#!/bin/bash

echo "=== Deploying ConceptDigger ==="

# ensure shared ss-network exists
if ! docker network ls | grep -q "ss-network"; then
  docker network create ss-network
fi

# ensure letsencrypt directory and acme.json exist with strict permissions
mkdir -p letsencrypt
touch letsencrypt/acme.json
chmod 600 letsencrypt/acme.json

# build and restart containers using both compose files
docker compose -f docker-compose.yml -f docker-compose-traefik.yml down
docker compose -f docker-compose.yml -f docker-compose-traefik.yml up -d --build --remove-orphans

echo "=== ConceptDigger Deployed Successfully! ==="