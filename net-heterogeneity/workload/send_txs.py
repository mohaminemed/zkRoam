#!/usr/bin/env python3
"""
Sends simple value-transfer transactions against one node's RPC endpoint at a
target rate, and logs submission time, inclusion time, and the block each tx
landed in — used to compute throughput (tx confirmed / s) and per-tx latency
(inclusion_time - submission_time).

Usage:
    python3 send_txs.py --rpc http://localhost:8545 --num-tx 500 --rate 20 \
        --out results/run_500tx.csv

Requires: pip install web3
"""
import argparse
import csv
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from web3 import Web3

# Well-known pre-funded dev account from the QBFT genesis alloc (see
# 01-generate-network.sh output / genesis.json "alloc" section). Replace with
# your funded account's private key.
SENDER_PRIVATE_KEY = "0x8f2a55949038a9610f50fb23b5883af3b4ecb3c3bb792cbcefbd1542c692f63"


def send_one(w3: Web3, sender, nonce, chain_id, receiver, tx_index):
    submit_ts = time.time()
    tx = {
        "from": sender.address,
        "to": receiver,
        "value": 1,
        "gas": 21000,
        "gasPrice": 0,
        "nonce": nonce,
        "chainId": chain_id,
    }
    signed = sender.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    confirm_ts = time.time()

    return {
        "tx_index": tx_index,
        "tx_hash": tx_hash.hex(),
        "submit_ts": submit_ts,
        "confirm_ts": confirm_ts,
        "latency_s": confirm_ts - submit_ts,
        "block_number": receipt.blockNumber,
        "status": receipt.status,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", required=True, help="e.g. http://localhost:8545")
    ap.add_argument("--num-tx", type=int, default=500)
    ap.add_argument("--rate", type=float, default=20.0, help="target tx/s submission rate")
    ap.add_argument("--out", required=True, help="output CSV path")
    ap.add_argument(
        "--receiver",
        default="0x0000000000000000000000000000000000dEaD",
        help="destination address (any valid address works for a value transfer)",
    )
    args = ap.parse_args()

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        raise SystemExit(f"Could not connect to {args.rpc}")

    chain_id = w3.eth.chain_id
    sender = w3.eth.account.from_key(SENDER_PRIVATE_KEY)
    start_nonce = w3.eth.get_transaction_count(sender.address)

    inter_arrival = 1.0 / args.rate
    results = []

    print(f"Submitting {args.num_tx} tx at target {args.rate} tx/s to {args.rpc} "
          f"(chain_id={chain_id})...")

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = []
        for i in range(args.num_tx):
            futures.append(
                pool.submit(send_one, w3, sender, start_nonce + i, chain_id,
                            args.receiver, i)
            )
            time.sleep(inter_arrival)

        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                print(f"tx failed: {e}")

    results.sort(key=lambda r: r["tx_index"])

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["tx_index", "tx_hash", "submit_ts", "confirm_ts",
                        "latency_s", "block_number", "status"],
        )
        writer.writeheader()
        writer.writerows(results)

    if results:
        span = max(r["confirm_ts"] for r in results) - min(r["submit_ts"] for r in results)
        throughput = len(results) / span if span > 0 else float("nan")
        avg_latency = sum(r["latency_s"] for r in results) / len(results)
        max_latency = max(r["latency_s"] for r in results)
        print(f"Confirmed {len(results)}/{args.num_tx} tx")
        print(f"Throughput: {throughput:.2f} tx/s")
        print(f"Latency avg={avg_latency:.2f}s max={max_latency:.2f}s")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
