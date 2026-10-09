#!/usr/bin/env python3
"""
deploy_verifiers.py

Compiles and deploys:
  1. contracts/CDRVerifier.sol            (individual Groth16 proof verifier)
  2. contracts/SnarkPackAggregateAnchor.sol (aggregate-proof anchor contract)

against one of the Besu QBFT RPC endpoints in your topology.json, using the
first funded account in accounts.json as the deployer.

Writes deployed_contracts.json:
    {
      "chain_id": ...,
      "cdr_verifier": {"address": "0x...", "abi": [...]},
      "aggregate_anchor": {"address": "0x...", "abi": [...]}
    }

zkroam_workload.py reads this file automatically (via
--deployed-contracts, default deployed_contracts.json) so the "individual"
and "aggregate" legs call real deployed contracts instead of a bare
--verifier-address with raw calldata.

REQUIREMENTS
------------
    pip install py-solc-x web3 --break-system-packages
    python3 -c "import solcx; solcx.install_solc('0.8.19')"

    (solc is downloaded from binaries.soliditylang.org - if your network
    blocks that, install solc via your OS package manager / Foundry
    instead and point SOLC_BINARY at it.)
"""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import solcx
from web3 import Web3

from besu_benchmark import build_rpc_endpoints, connect_web3, load_accounts, load_json


SOLC_VERSION = "0.8.19"


def compile_contract(path, contract_name, solc_version=SOLC_VERSION):
    solcx.install_solc(solc_version, show_progress=False)
    out = solcx.compile_files(
        [path],
        solc_version=solc_version,
        output_values=["abi", "bin"],
    )
    # key is "<path>:<ContractName>"
    for key, artifact in out.items():
        if key.endswith(f":{contract_name}"):
            return artifact["abi"], artifact["bin"]
    raise RuntimeError(f"Contract {contract_name} not found in {path}")


def deploy(w3, deployer, abi, bytecode, constructor_args=None):
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    tx = contract.constructor(*(constructor_args or [])).build_transaction(
        {
            "chainId": w3.eth.chain_id,
            "from": deployer["address"],
            "nonce": w3.eth.get_transaction_count(deployer["address"], "pending"),
            "gas": 6_000_000,
            "gasPrice": w3.eth.gas_price,
        }
    )

    signed = w3.eth.account.sign_transaction(tx, deployer["private_key"])
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    print(f"  submitted deploy tx {tx_hash.hex()}, waiting for receipt...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status != 1:
        raise RuntimeError(f"Deployment reverted: {receipt}")

    print(f"  deployed at {receipt.contractAddress}  "
          f"(gasUsed={receipt.gasUsed})")

    return receipt.contractAddress


def call_tx(w3, sender, to_address, abi, fn_name, args, gas=200_000):
    """Send a plain state-changing call (e.g. addVerifier) and wait for it."""
    contract = w3.eth.contract(address=to_address, abi=abi)
    tx = contract.functions[fn_name](*args).build_transaction(
        {
            "chainId": w3.eth.chain_id,
            "from": sender["address"],
            "nonce": w3.eth.get_transaction_count(sender["address"], "pending"),
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
        }
    )
    signed = w3.eth.account.sign_transaction(tx, sender["private_key"])
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status != 1:
        raise RuntimeError(f"{fn_name}({args}) reverted: {receipt}")
    return receipt


def add_verifiers_parallel(web3s, deployer, anchor_address, abi, verifier_addresses):
    """
    Authorize each address in verifier_addresses via addVerifier(), round-
    robining submission across ALL available RPC endpoints and submitting
    them concurrently instead of one-at-a-time through web3s[0].

    addVerifier is onlyOwner, so every one of these txs still has to come
    from the SAME deployer account - nonces are computed once up front and
    assigned sequentially locally (not re-queried per call), which is what
    makes concurrent submission safe: no two txs race for the same nonce.
    Submitting the pre-signed batch to different nodes just spreads the
    network/RPC-handling overhead instead of paying it serially; Besu's
    P2P gossip propagates any tx to every validator well within normal
    QBFT block times regardless of which node it was first submitted to,
    so nonce ordering across nodes isn't a concern here.
    """
    if not verifier_addresses:
        return []

    w3_head = web3s[0]
    start_nonce = w3_head.eth.get_transaction_count(deployer["address"], "pending")
    gas_price = w3_head.eth.gas_price
    chain_id = w3_head.eth.chain_id
    template = w3_head.eth.contract(address=anchor_address, abi=abi)

    # Build + sign everything up front - this is what lets submission be
    # fully concurrent below, no per-tx round trip to fetch a nonce.
    jobs = []
    for i, addr in enumerate(verifier_addresses):
        w3 = web3s[i % len(web3s)]
        tx = template.functions.addVerifier(addr).build_transaction({
            "chainId": chain_id,
            "from": deployer["address"],
            "nonce": start_nonce + i,
            "gas": 200_000,
            "gasPrice": gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, deployer["private_key"])
        jobs.append({"w3": w3, "addr": addr, "raw": signed.raw_transaction, "tx_hash": None})

    def submit(job):
        job["tx_hash"] = job["w3"].eth.send_raw_transaction(job["raw"])
        return job

    with ThreadPoolExecutor(max_workers=len(web3s)) as ex:
        for job in ex.map(submit, jobs):
            print(f"  submitted addVerifier({job['addr']}) -> {job['tx_hash'].hex()} "
                  f"via {job['w3'].provider.endpoint_uri}")

    def wait(job):
        receipt = job["w3"].eth.wait_for_transaction_receipt(job["tx_hash"], timeout=120)
        return job["addr"], receipt

    confirmed = []
    with ThreadPoolExecutor(max_workers=len(web3s)) as ex:
        futures = [ex.submit(wait, job) for job in jobs]
        for f in as_completed(futures):
            addr, receipt = f.result()
            if receipt.status != 1:
                raise RuntimeError(f"addVerifier({addr}) reverted: {receipt}")
            confirmed.append(addr)
            print(f"  confirmed addVerifier({addr}) in block {receipt.blockNumber}")

    return confirmed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topology", default="networkFiles/topology.json")
    parser.add_argument("--accounts", default="networkFiles/accounts/accounts.json")
    parser.add_argument("--contracts-dir", default="contracts")
    parser.add_argument("--out", default="deployed_contracts.json")
    parser.add_argument(
        "--skip-cdr-verifier", action="store_true",
        help="Skip deploying CDRVerifier.sol (e.g. curve mismatch not resolved yet).",
    )
    parser.add_argument(
        "--verifier-pool-size", type=int, default=1,
        help=(
            "How many accounts to authorize as verifiers on "
            "SnarkPackAggregateAnchor (accounts[0] is always the deployer + "
            "first verifier from the constructor; this adds accounts[1:N] "
            "via addVerifier()). NOTE: with relayAggregateProof, verifier "
            "identity is decoupled from transaction-submitting nonce - "
            "this pool no longer needs to be large for throughput reasons "
            "(that's now handled by relaying through the full account "
            "pool, see zkroam_workload.py). Size this for how "
            "many independent trusted verifier keys you actually want "
            "(redundancy / threshold-of-N), not for nonce-gap capacity."
        ),
    )
    args = parser.parse_args()

    topology = load_json(args.topology)
    endpoints = build_rpc_endpoints(topology)
    web3s, chain_id = connect_web3(endpoints)
    w3 = web3s[0]  # deploy via the first node; any node broadcasts to all validators

    accounts = load_accounts(args.accounts)
    deployer = accounts[0]
    print(f"\nDeploying from {deployer['address']} via {endpoints[0]['url']} "
          f"(chain_id={chain_id})\n")

    result = {"chain_id": chain_id}

    if not args.skip_cdr_verifier:
        print("Compiling CDRVerifier.sol ...")
        abi, bin_ = compile_contract(
            os.path.join(args.contracts_dir, "CDRVerifier.sol"),
            "Groth16Verifier",
        )
        print("Deploying CDRVerifier ...")
        addr = deploy(w3, deployer, abi, bin_)
        result["cdr_verifier"] = {"address": addr, "abi": abi}
    else:
        print("Skipping CDRVerifier.sol (--skip-cdr-verifier)")

    print("\nCompiling SnarkPackAggregateAnchor.sol ...")
    abi, bin_ = compile_contract(
        os.path.join(args.contracts_dir, "SnarkPackAggregateAnchor.sol"),
        "SnarkPackAggregateAnchor",
    )
    print("Deploying SnarkPackAggregateAnchor ...")
    addr = deploy(w3, deployer, abi, bin_, constructor_args=[deployer["address"]])

    pool_size = max(1, min(args.verifier_pool_size, len(accounts)))
    verifier_pool = [accounts[0]["address"]]

    if pool_size > 1:
        candidates = [accounts[i]["address"] for i in range(1, pool_size)]
        print(f"\nAuthorizing {len(candidates)} additional verifier(s) "
              f"(pool size {pool_size}) across {len(web3s)} endpoint(s) ...")
        confirmed = add_verifiers_parallel(web3s, deployer, addr, abi, candidates)
        verifier_pool.extend(confirmed)
    else:
        print(
            "\nverifier-pool-size=1: only accounts[0] is authorized as a "
            "verifier - fine as-is if you're relaying via "
            "relayAggregateProof (submission load spreads across the full "
            "account pool regardless of verifier count). Only increase "
            "this if you want multiple independent verifier keys for "
            "redundancy/threshold, or if you plan to use the direct "
            "anchorAggregateProof(onlyVerifier) path at any real volume."
        )

    result["aggregate_anchor"] = {
        "address": addr,
        "abi": abi,
        "verifier_addresses": verifier_pool,
    }

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nWrote: {args.out}")


if __name__ == "__main__":
    main()