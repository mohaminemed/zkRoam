#!/usr/bin/env bash
# End-to-end (heterogeneous): generate keys/genesis -> assign core/edge/mobile tiers -> generate compose -> launch.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

echo "=== Step 0: tear down any previous experiment ==="
docker compose down -v --remove-orphans 2>/dev/null || true
docker rm -f $(docker ps -aq --filter "name=^node[0-9][0-9]$") 2>/dev/null || true
docker network prune -f >/dev/null
rm -rf networkFiles/tc
if ! rm -rf logs 2>/dev/null; then
  docker run --rm -v "$PWD":/w alpine rm -rf /w/logs
fi
mkdir -p results
# Pre-create per-node log dirs as YOUR user so Docker doesn't create them as root
for i in $(seq -w 0 14); do mkdir -p "logs/node${i}"; done
chmod -R 777 logs

echo "=== Step 1: generate genesis + validator keys ==="
bash scripts/01-generate-network.sh

echo "=== Step 2: assign nodes to core/edge/mobile tiers ==="
python3 scripts/02-assign-tiers.py

echo "=== Step 3: generate docker-compose.yml ==="
python3 scripts/03-generate-compose.py

echo "=== Step 4: build + launch 15-node network ==="
docker compose build --no-cache
docker compose up -d

echo "=== Waiting for node00 RPC (up to 120s) ==="
ok=0
for i in $(seq 1 60); do
  if curl -s -m 2 -X POST -H "Content-Type: application/json" \
      --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
      http://localhost:8545 >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 2
done

if [ "${ok}" -ne 1 ]; then
  echo "ERROR: node00 RPC never came up. Container states:"
  docker compose ps -a
  echo "---- docker logs node00 ----"
  docker logs node00 2>&1 | tail -30
  exit 1
fi

echo "=== Letting QBFT settle ==="
sleep 20

echo "=== Node00 (core) block number: ==="
curl -s -X POST -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
  http://localhost:8545 | python3 -m json.tool || true

echo "Network is up."
