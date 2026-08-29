#!/usr/bin/env python3
"""
Post-run analysis for the heterogeneous QBFT experiment.

1. View changes: Besu logs a line containing "Round change" / "RoundChange"
   whenever a validator times out waiting for a proposal or gives up on a
   round — this is the direct signal of network stress causing consensus
   instability. We grep each node's log and count these events over the run.

2. Time-to-finality: for QBFT (single-slot deterministic finality, no forks),
   a block is final as soon as it's imported with a valid seal. We read block
   timestamps via RPC and compare consecutive block times to the configured
   block period to flag blocks that took longer than expected — an indirect
   sign of the mobile tier's loss/delay stalling proposal rounds.

Usage:
    python3 collect_metrics.py --logs-dir ../logs --rpc http://localhost:8545 \
        --out results/robustness_summary.csv
"""
import argparse
import csv
import glob
import os
import re

from web3 import Web3

ROUND_CHANGE_PATTERN = re.compile(r"round\s*change", re.IGNORECASE)


def count_view_changes(logs_dir: str) -> dict:
    counts = {}
    for log_path in glob.glob(os.path.join(logs_dir, "*", "*.log")):
        node_name = os.path.basename(os.path.dirname(log_path))
        n = 0
        with open(log_path, errors="ignore") as f:
            for line in f:
                if ROUND_CHANGE_PATTERN.search(line):
                    n += 1
        counts[node_name] = counts.get(node_name, 0) + n
    return counts


def block_finality_stats(rpc_url: str, block_period_s: int = 2, lookback: int = 500):
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise SystemExit(f"Could not connect to {rpc_url}")

    latest = w3.eth.block_number
    start = max(0, latest - lookback)

    rows = []
    prev_ts = None
    for bn in range(start, latest + 1):
        block = w3.eth.get_block(bn)
        gap = None if prev_ts is None else block.timestamp - prev_ts
        rows.append({
            "block_number": bn,
            "timestamp": block.timestamp,
            "gap_s": gap,
            "delayed": bool(gap is not None and gap > block_period_s * 2),
        })
        prev_ts = block.timestamp

    delayed_blocks = sum(1 for r in rows if r["delayed"])
    return rows, delayed_blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs-dir", required=True, help="directory containing logs/<node>/*.log")
    ap.add_argument("--rpc", required=True, help="RPC endpoint of any node")
    ap.add_argument("--block-period-s", type=int, default=2)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    view_changes = count_view_changes(args.logs_dir)
    block_rows, delayed_blocks = block_finality_stats(args.rpc, args.block_period_s)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        writer.writerow(["total_view_changes_all_nodes", sum(view_changes.values())])
        for node, n in sorted(view_changes.items()):
            writer.writerow([f"view_changes_{node}", n])
        writer.writerow(["blocks_analyzed", len(block_rows)])
        writer.writerow(["blocks_exceeding_2x_block_period", delayed_blocks])
        if block_rows:
            gaps = [r["gap_s"] for r in block_rows if r["gap_s"] is not None]
            if gaps:
                writer.writerow(["avg_block_gap_s", sum(gaps) / len(gaps)])
                writer.writerow(["max_block_gap_s", max(gaps)])

    print(f"Total view changes across all nodes: {sum(view_changes.values())}")
    print(f"Blocks exceeding 2x block period (stall indicator): "
          f"{delayed_blocks}/{len(block_rows)}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
