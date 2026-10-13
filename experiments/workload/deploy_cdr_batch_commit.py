#!/usr/bin/env python3
"""
Deploy CDRBatchCommitment and register it as "cdr_batch_commit" in
deployed_contracts.json (same {"address": ..., "tx_hash": ...} shape the
workload reads for "cdr_commit").

Compiles with py-solc-x if installed, else the `solc` binary on PATH.
Place CDRBatchCommitment.sol at contracts/CDRBatchCommitment.sol or pass --source.

  python3 workload/deploy_cdr_batch_commit.py \
      --topology networkFiles/topology.json --accounts accounts/accounts.json \
      --out deployed_contracts.json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from besu_benchmark import (  # noqa: E402
    build_rpc_endpoints, connect_web3, load_accounts, load_json,
)
from web3 import Web3  # noqa: E402


def compile_contract(source, evm_version):
    name = "CDRBatchCommitment"
    try:
        import solcx
        out = solcx.compile_files([source], output_values=["abi", "bin"],
                                  evm_version=evm_version)
        key = next(k for k in out if k.endswith(":" + name))
        return out[key]["abi"], out[key]["bin"]
    except ImportError:
        pass
    solc = shutil.which("solc")
    if not solc:
        raise RuntimeError("need py-solc-x (pip install py-solc-x) or solc on PATH")
    res = subprocess.run(
        [solc, "--evm-version", evm_version, "--combined-json", "abi,bin", source],
        check=True, capture_output=True, text=True)
    data = json.loads(res.stdout)["contracts"]
    key = next(k for k in data if k.endswith(":" + name))
    abi = data[key]["abi"]
    return (json.loads(abi) if isinstance(abi, str) else abi), data[key]["bin"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topology", required=True)
    ap.add_argument("--accounts", required=True)
    ap.add_argument("--out", default="deployed_contracts.json")
    ap.add_argument("--source", default="contracts/CDRBatchCommitment.sol")
    ap.add_argument("--evm-version", default="paris",
                    help="paris avoids PUSH0 on pre-Shanghai Besu genesis configs")
    ap.add_argument("--key-index", type=int, default=0, help="deployer account index")
    a = ap.parse_args()

    abi, bytecode = compile_contract(a.source, a.evm_version)

    endpoints = build_rpc_endpoints(load_json(a.topology))
    web3s, chain_id = connect_web3(endpoints)
    w3 = web3s[0]
    acct = load_accounts(a.accounts)[a.key_index]
    addr = Web3.to_checksum_address(acct["address"])

    contract = w3.eth.contract(abi=abi, bytecode="0x" + bytecode.removeprefix("0x"))
    tx = contract.constructor().build_transaction({
        "chainId": chain_id, "from": addr,
        "nonce": w3.eth.get_transaction_count(addr, "pending"),
        "gasPrice": w3.eth.gas_price,
    })
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = w3.eth.account.sign_transaction(tx, acct["private_key"])
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    if rcpt["status"] != 1:
        raise RuntimeError(f"deployment reverted: {h.hex()}")

    deployed = load_json(a.out) if os.path.exists(a.out) else {}
    deployed["cdr_batch_commit"] = {
        "address": rcpt["contractAddress"], "tx_hash": h.hex(),
        "gas_used": rcpt["gasUsed"],
    }
    with open(a.out, "w") as f:
        json.dump(deployed, f, indent=2)
    print(f"CDRBatchCommitment deployed at {rcpt['contractAddress']} "
          f"(gas {rcpt['gasUsed']}) -> {a.out}")


if __name__ == "__main__":
    main()
