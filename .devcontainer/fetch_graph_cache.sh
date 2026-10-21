#!/usr/bin/env bash
set -e

TARGET="data/graph_cache.json"
TEMP_TARGET="$TARGET.download"

mkdir -p data

if [ -f "$TARGET" ]; then
    echo "graph_cache.json already exists in data dir, skipping download."
    exit 0
fi

if [ -z "$GRAPH_CACHE_URL" ]; then
    echo "GRAPH_CACHE_URL is not set in env, skipping download."
    exit 0
fi

echo "Downloading graph_cache.json..."
gdown --fuzzy "$GRAPH_CACHE_URL" -O "$TEMP_TARGET"

# Validate that the downloaded file is a valid JSON file before installing it
if python3 -c "import json; json.load(open('$TEMP_TARGET'))" 2>/dev/null; then
    mv "$TEMP_TARGET" "$TARGET"
    echo "Done. graph_cache.json successfully downloaded and validated."
else
    echo "Error: Downloaded file is not valid JSON (possibly an HTML page or corrupted link)." >&2
    rm -f "$TEMP_TARGET"
    exit 1
fi