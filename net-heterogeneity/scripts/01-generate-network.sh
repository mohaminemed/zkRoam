#!/usr/bin/env bash
# Generates genesis.json + per-node keypairs for a 24-validator QBFT network.
# Requires Docker (uses the official hyperledger/besu image, no local install needed).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BESU_IMAGE="hyperledger/besu:24.7.0"

echo "[1/2] Generating QBFT genesis file and validator keys..."

rm -rf "${ROOT_DIR}/networkFiles"
mkdir -p "${ROOT_DIR}/networkFiles"

docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "${ROOT_DIR}/config:/config:ro" \
  -v "${ROOT_DIR}/networkFiles:/networkFiles" \
  "${BESU_IMAGE}" \
  operator generate-blockchain-config \
    --config-file=/config/qbftConfigFile.json \
    --to=/networkFiles \
    --private-key-file-name=key

echo "[2/2] Done. Validator directories are under networkFiles/keys/<address>/"

ls "${ROOT_DIR}/networkFiles/keys" | head -5
echo "... (24 total)"