#!/bin/sh
set -e

CONFIG=/data/options.json

if [ ! -f "$CONFIG" ]; then
    echo "[ERROR] No config at $CONFIG"
    exit 1
fi

export SPOTIFY_CLIENT_ID=$(python3 -c "import json; print(json.load(open('$CONFIG'))['client_id'])")
export SPOTIFY_CLIENT_SECRET=$(python3 -c "import json; print(json.load(open('$CONFIG'))['client_secret'])")
export HA_URL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['ha_url'])")
export HA_TOKEN=$(python3 -c "import json; print(json.load(open('$CONFIG'))['ha_token'])")
export API_PORT=$(python3 -c "import json; print(json.load(open('$CONFIG'))['api_port'])")
export SONOS_ENTITY=$(python3 -c "import json; print(json.load(open('$CONFIG'))['sonos_entity'])")

echo "[INFO] Spotify Smart DJ v0.1.0"
echo "[INFO] API port: ${API_PORT}"
echo "[INFO] Sonos: ${SONOS_ENTITY}"

exec python3 /app/server.py
