#!/usr/bin/env bash
# Health-check every candidate node with mihomo's group delay test, then
# report the alive set to the subscription Worker (see tools/collect.py).
set -euo pipefail
cd "$(dirname "$0")/.."

MIHOMO_VER="v1.19.33"
if [ ! -x ./mihomo ]; then
  curl -sL --max-time 300 -o mihomo.gz \
    "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VER}/mihomo-linux-amd64-${MIHOMO_VER}.gz"
  gunzip -f mihomo.gz && chmod +x ./mihomo
fi

python3 tools/build_config.py
./mihomo -d . -f config.yaml > mihomo.log 2>&1 &
MIHOMO_PID=$!
trap 'kill $MIHOMO_PID 2>/dev/null || true' EXIT

for i in $(seq 1 30); do
  if curl -s --max-time 3 http://127.0.0.1:9090/version >/dev/null; then break; fi
  sleep 1
done

# One call tests the whole group concurrently; generous ceiling for ~1000 nodes.
curl -s --max-time 900 -o delays.json \
  'http://127.0.0.1:9090/group/CHECK/delay?url=http://www.gstatic.com/generate_204&timeout=8000'
echo "delay result bytes: $(wc -c < delays.json)"

python3 tools/collect.py
