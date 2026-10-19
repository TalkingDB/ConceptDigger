#!/usr/bin/env bash
# Builds and runs ConceptDigger in a container on port 5007, with the
# JSON graph cache persisted to ./data on the host so it survives restarts.
set -euo pipefail

IMAGE_NAME="concept-digger"
CONTAINER_NAME="concept-digger"
PORT="${PORT:-5007}"
SPARQL_ENDPOINT="${SPARQL_ENDPOINT:-https://dbpedia.org/sparql}"

cd "$(dirname "$0")"
mkdir -p data

docker build -t "$IMAGE_NAME" .

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

MSYS_NO_PATHCONV=1 docker run -d \
  --name "$CONTAINER_NAME" \
  -p "${PORT}:5007" \
  -v "$(pwd)/data:/app/data" \
  -e SPARQL_ENDPOINT="${SPARQL_ENDPOINT}" \
  -e CACHE_FILE_PATH="data/graph_cache.json" \
  "$IMAGE_NAME"

echo "ConceptDigger is running at http://localhost:${PORT}"
echo "Health check: curl http://localhost:${PORT}/api/v1/health"
