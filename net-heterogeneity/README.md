# QBFT Network Heterogeneity Experiment

24 QBFT validators (Hyperledger Besu) split into three tiers, each shaped
with `tc netem` inside its own Docker container on a shared bridge network
(`gurubft-net`), plus a control (homogeneous) run for comparison.

| Tier   | Count | delay      | rate     | loss | jitter |
|--------|-------|------------|----------|------|--------|
| core   | 6     | 10ms       | 100Mbit  | 0%   | 0ms    |
| edge   | 8     | 100ms      | 50Mbit   | 0%   | 10ms   |
| mobile | 10    | 300ms      | 10Mbit   | 5%   | 50ms   |

## Prerequisites
- Docker + Docker Compose v2
- Python 3.9+, `pip install web3`

## Layout
```
config/qbftConfigFile.json     genesis + validator-generation spec (24 nodes, pre-funded dev account)
scripts/01-generate-network.sh generates genesis.json + 24 validator keypairs (via Besu image)
scripts/02-assign-tiers.py     assigns nodes to core/edge/mobile + IPs -> networkFiles/topology.json
scripts/02b-assign-tiers-baseline.py   control variant: all 24 nodes on the core profile
scripts/03-generate-compose.py generates docker-compose.yml from topology.json
scripts/04-run-network.sh      one-shot: runs 01-03 then `docker compose up`
docker/Dockerfile              Besu image + iproute2 (tc)
docker/entrypoint.sh           applies tc netem, then launches besu, logging to /data/logs
workload/send_txs.py           submits tx at a target rate, logs per-tx latency
monitoring/collect_metrics.py  counts QBFT round-changes + flags stalled blocks (finality proxy)
```

## Running the heterogeneous experiment
```bash
bash scripts/04-run-network.sh
python workload/send_txs.py \
    --topology networkFiles/topology.json \
    --accounts accounts/accounts.json \
    --num-tx 1000 \
    --rate 500 \
    --workers 48 \
    --receipt-workers 32 \
    --out results/hetero_baseline_1000tx_500.csv
    --summary-out results/summary_hetero_1000tx_500.csv
python3 monitoring/collect_metrics.py --logs-dir ./logs --rpc http://localhost:8545 --out results/hetero_robustness.csv
docker compose down
```

## Running the baseline (homogeneous, "ideal network") control
```bash
bash scripts/04b-run-baseline.sh

python3 workload/send_txs.py \
    --topology networkFiles/topology.json \
    --accounts accounts/accounts.json \
    --num-tx 1000 \
    --rate 500 \
    --workers 48 \
    --receipt-workers 32 \
    --out results/details_baseline_1000tx_500.csv
    --summary-out results/summary_baseline_1000tx_500.csv
python3 monitoring/collect_metrics.py --logs-dir ./logs --rpc http://localhost:8545 --out results/baseline_robustness.csv
docker compose down
```

## Metrics captured
- **Throughput / latency** (`send_txs.py`): confirmed tx/s and per-tx
  submit-to-inclusion latency, same style as the groth16/ultraVerifier plots
  earlier in the paper.
- **View changes** (`collect_metrics.py`): count of QBFT round-change events
  per node, parsed from Besu logs — the direct signal that mobile-tier
  delay/loss is destabilizing consensus, not just slowing it down.
- **Block gap / stall indicator** (`collect_metrics.py`): flags blocks whose
  inter-arrival time exceeds 2x the configured block period
  (`blockperiodseconds` in `qbftConfigFile.json`), as a proxy for
  degraded time-to-finality.

## Notes / things to double check before running for real
- `SENDER_PRIVATE_KEY` in `workload/send_txs.py` must match the account
  pre-funded in `config/qbftConfigFile.json`'s `alloc` section (already
  aligned in this template — change both together if you regenerate keys).
- `01-generate-network.sh` pins `hyperledger/besu:24.7.0`; bump the tag in
  both that script and `docker/Dockerfile` together if you need a newer
  Besu release.
- Bootnodes are the 3 core-tier nodes; if you want to stress-test bootstrap
  itself under loss, point bootnodes at mobile-tier nodes instead.
- Repeat each run 3-5x per transaction count (mirroring the 10% std-dev
  reporting used elsewhere in the paper) — these scripts run a single trial;
  wrap `04-run-network.sh` + workload + `docker compose down` in a loop for
  repeated trials.
