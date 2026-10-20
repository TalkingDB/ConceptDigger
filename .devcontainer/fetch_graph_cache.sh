#!/usr/bin/env bash
set -e

TARGET="data/graph_cache.json"

mkdir -p data

if [ -f "$TARGET" ]; then
    echo "graph_cache.json already exists id data dir, skipping download."
    exit 0
fi

if [ -z "$GRAPH_CACHE_URL" ]; then
    echo "GRAPH_CACHE_URL is not set in env, skipping download."
    exit 0
fi

echo "Downloading graph_cache.json..."
gdown "$GRAPH_CACHE_URL" -O "$TARGET.tmp"
mv "$TARGET.tmp" "$TARGET"
echo "Done."