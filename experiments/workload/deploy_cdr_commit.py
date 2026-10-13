#!/usr/bin/env python3
"""
deploy_cdr_commit.py

Compiles contracts/CDRCommitment.sol (target EVM: london by default), deploys it on the Besu network and merges
{"cdr_commit": {"address": ..., "deploy_gas_used": ...}} into
deployed_contracts.json (the file deploy_verifiers.py already writes), so
zkroam_workload.py picks it up automatically.

    pip install py-solc-x
    python3 workload/deploy_cdr_commit.py \\
        --topology networkFiles/topology.json --accounts accounts/accounts.json
"""

import argparse
import json
import os

import solcx
import yaml
from web3 import Web3

from besu_benchmark import build_rpc_endpoints, connect_web3, load_accounts, load_json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None,
                    help="optional YAML config; supplies defaults for the paths below")
    ap.add_argument("--topology", default=None)
    ap.add_argument("--accounts", default=None)
    ap.add_argument("--out", default=None,
                    help="deployed_contracts.json to update (same file deploy_verifiers.py writes)")
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)  # experiments/
    default_sol = os.path.join(root, "contracts", "CDRCommitment.sol")
    if not os.path.exists(default_sol):
        default_sol = os.path.join(here, "CDRCommitment.sol")
    ap.add_argument("--sol", default=default_sol)
    ap.add_argument("--solc-version", default="0.8.24")
    ap.add_argument("--evm-version", default="london",
                    help="target EVM; must be supported by the Besu genesis. solc 0.8.24 "
                         "defaults to cancun (PUSH0 etc.), which older forks reject with "
                         "INVALID_OPERATION")
    ap.add_argument("--deployer-index", type=int, default=0)
    args = ap.parse_args()

    cfg = {}
    if args.config:
        with open(args.config) as f:
            cfg = yaml.safe_load(f) or {}
    topology = args.topology or cfg.get("network", {}).get("topology", "networkFiles/topology.json")
    accounts_path = args.accounts or cfg.get("network", {}).get("accounts", "accounts/accounts.json")
    out_path = args.out or cfg.get("contracts", {}).get("deployed_contracts", "deployed_contracts.json")

    solcx.install_solc(args.solc_version)
    compiled = solcx.compile_files(
        [args.sol], output_values=["abi", "bin"],
        solc_version=args.solc_version, optimize=True, optimize_runs=200,
        evm_version=args.evm_version,
    )
    _, art = next(iter(compiled.items()))

    endpoints = build_rpc_endpoints(load_json(topology))
    web3s, chain_id = connect_web3(endpoints)
    w3 = web3s[0]
    acct = load_accounts(accounts_path)[args.deployer_index]

    contract = w3.eth.contract(abi=art["abi"], bytecode=art["bin"])
    tx = contract.constructor().build_transaction({
        "chainId": chain_id,
        "nonce": w3.eth.get_transaction_count(acct["address"]),
        "gasPrice": w3.eth.gas_price,
        "from": acct["address"],
    })
    signed = w3.eth.account.sign_transaction(tx, acct["private_key"])
    # Broadcast the signed deployment transaction and retrieve its hash.
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError("CDRCommitment deployment reverted")

    deployed = load_json(out_path) if os.path.exists(out_path) else {}
    deployed["cdr_commit"] = {
        "address": Web3.to_checksum_address(receipt["contractAddress"]),
        "deploy_gas_used": receipt["gasUsed"],
    }
    with open(out_path, "w") as f:
        json.dump(deployed, f, indent=2)

    print(f"CDRCommitment at {receipt['contractAddress']} "
          f"(deploy gas {receipt['gasUsed']}) -> {out_path}")


if __name__ == "__main__":
    main()