#!/usr/bin/env bash
# End-to-end: generate keys/genesis -> assign tiers -> generate compose -> launch.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

echo "=== Step 1: generate genesis + validator keys ==="
bash scripts/01-generate-network.sh

echo "=== Step 2: assign nodes to core/edge/mobile tiers ==="
python3 scripts/02-assign-tiers.py

echo "=== Step 3: generate docker-compose.yml ==="
python3 scripts/03-generate-compose.py

echo "=== Step 4: build + launch 24-node network ==="
mkdir -p logs results
docker compose up -d --build

echo "=== Waiting 30s for QBFT validators to reach consensus... ==="
sleep 30

echo "=== Node00 (core) block number: ==="
curl -s -X POST -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
  http://localhost:8545 | python3 -m json.tool

echo ""
echo "Network is up. Example next steps:"
echo "  python3 workload/send_txs.py --rpc http://localhost:8545 --num-tx 500 --rate 20 --out results/run_500tx.csv"
echo "  python3 monitoring/collect_metrics.py --logs-dir ./logs --rpc http://localhost:8545 --out results/robustness_summary.csv"
echo "  docker compose down   # to tear down"
