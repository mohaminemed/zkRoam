#!/usr/bin/env python3
"""
Baseline / control variant of 02-assign-tiers.py: every node gets the CORE
profile (low latency, high bandwidth, no loss). Run this instead of
02-assign-tiers.py, then continue with 03-generate-compose.py, to produce
the homogeneous "ideal network" baseline that the heterogeneous result
should be compared against.

    python3 scripts/02b-assign-tiers-baseline.py
    python3 scripts/03-generate-compose.py
    docker compose up -d --build
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYS_DIR = os.path.join(ROOT, "networkFiles", "keys")
OUT_FILE = os.path.join(ROOT, "networkFiles", "topology.json")

SUBNET_PREFIX = "172.28.0"
BASE_HOST_RPC_PORT = 8545
BASE_HOST_WS_PORT = 8546
P2P_PORT = 30303

CORE_PROFILE = {"delay_ms": 10, "rate_mbit": 100, "loss_pct": 0, "jitter_ms": 0}


def main():
    addresses = sorted(os.listdir(KEYS_DIR))
    if len(addresses) != 24:
        raise SystemExit(f"Expected 24 validator keys, found {len(addresses)}.")

    nodes = []
    for i, addr in enumerate(addresses):
        nodes.append({
            "index": i,
            "name": f"node{i:02d}",
            "tier": "core",  # everyone gets the ideal profile in the baseline
            "address": addr,
            "ip": f"{SUBNET_PREFIX}.{10 + i}",
            "p2p_port": P2P_PORT,
            "host_rpc_port": BASE_HOST_RPC_PORT + i,
            "host_ws_port": BASE_HOST_WS_PORT + 30 + i,
            "profile": CORE_PROFILE,
        })

    with open(OUT_FILE, "w") as f:
        json.dump({"subnet": f"{SUBNET_PREFIX}.0/24", "nodes": nodes}, f, indent=2)

    print(f"Wrote baseline topology (24 core-profile nodes) to {OUT_FILE}")


if __name__ == "__main__":
    main()
