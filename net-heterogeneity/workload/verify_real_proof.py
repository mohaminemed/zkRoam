#!/usr/bin/env python3
"""
verify_real_proof.py

Standalone sanity check, separate from the full sweep: deploy
CDRVerifier.sol (or reuse an existing deploy_verifiers.py deployment)
and call verifyProof() with the REAL fixtures/proof.json +
fixtures/public.json, asserting it returns true.

This is the "single proof, reused, before we adjust anything else"
step: confirms the exact proof+public-signal wiring the workload
script uses is correct, with a real transaction against a real
contract, before spending time on the full nproofs x num_vmnos sweep.

IMPORTANT: fixtures/proof.json et al. verify against fixtures/
CDRCircuit.circom (Poseidon over 3 inputs: n_sms, n_mb, n_min; public
signal order [T, hashCDR, r_mb, r_sms, r_voice]). That is a DIFFERENT
circuit from src/constraints.rs (Poseidon over 5 inputs, including
randomness + session_id; public input order [r_sms, r_mb, r_voice, t,
hash_cdr]) - see the chat note on this. This script only proves that
the circom-generated verifier correctly verifies a circom-generated
proof. It does not (and cannot) verify anything produced by the Rust/
SnarkPack pipeline - those are two separate circuits.

Usage:
    python3 workload/verify_real_proof.py \
        --topology networkFiles/topology.json \
        --accounts accounts/accounts.json \
        --proof-json fixtures/proof.json \
        --public-json fixtures/public.json \
        --deployed-contracts deployed_contracts.json
"""

import argparse
import sys

from web3 import Web3

from besu_benchmark import build_rpc_endpoints, connect_web3, load_accounts, load_json
from zkroam_snarkpack_workload import (
    build_verify_proof_calldata,
    load_deployed_contracts,
    load_snarkjs_proof,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topology", default="networkFiles/topology.json")
    parser.add_argument("--accounts", default="accounts/accounts.json")
    parser.add_argument("--proof-json", default="fixtures/proof.json")
    parser.add_argument("--public-json", default="fixtures/public.json")
    parser.add_argument("--deployed-contracts", default="deployed_contracts.json")
    parser.add_argument(
        "--verifier-address", default=None,
        help="Skip deployed_contracts.json and use this address directly.",
    )
    parser.add_argument(
        "--send-tx", action="store_true",
        help="Also submit a real transaction (not just eth_call) and report gasUsed.",
    )
    args = parser.parse_args()

    topology = load_json(args.topology)
    endpoints = build_rpc_endpoints(topology)
    web3s, chain_id = connect_web3(endpoints)
    w3 = web3s[0]

    if args.verifier_address:
        verifier_address = Web3.to_checksum_address(args.verifier_address)
    else:
        deployed = load_deployed_contracts(args.deployed_contracts)
        if not deployed or not deployed.get("cdr_verifier"):
            print(
                f"error: no cdr_verifier in {args.deployed_contracts} and no "
                f"--verifier-address given. Run deploy_verifiers.py first.",
                file=sys.stderr,
            )
            sys.exit(1)
        verifier_address = Web3.to_checksum_address(deployed["cdr_verifier"]["address"])

    print(f"Verifier: {verifier_address}")
    print(f"Proof:    {args.proof_json}")
    print(f"Public:   {args.public_json}")

    pA, pB, pC, public_signals = load_snarkjs_proof(args.proof_json, args.public_json)
    print(f"Public signals (as loaded, in file order): {public_signals}")

    calldata_hex = build_verify_proof_calldata(w3, pA, pB, pC, public_signals)

    # 1) Cheap, no-gas correctness check via eth_call.
    result = w3.eth.call({"to": verifier_address, "data": calldata_hex})
    is_valid = int.from_bytes(result, "big") == 1

    print()
    print(f"eth_call verifyProof(...) -> {is_valid}")

    if not is_valid:
        print(
            "\nFAILED: the deployed contract did not accept this proof.\n"
            "Most likely causes:\n"
            "  - the deployed bytecode isn't actually built from this exact\n"
            "    verification_key.json (redeploy with deploy_verifiers.py)\n"
            "  - public.json's signal order doesn't match the circuit's declared\n"
            "    public order ([T, hashCDR, r_mb, r_sms, r_voice] for\n"
            "    fixtures/CDRCircuit.circom - do not substitute the arkworks\n"
            "    order [r_sms, r_mb, r_voice, t, hash_cdr], they differ)\n"
            "  - proof.json/public.json are stale relative to the deployed vk\n",
            file=sys.stderr,
        )
        sys.exit(1)

    print("OK: real proof verifies on-chain.")

    # 2) Optional: a real transaction, to see actual gasUsed for this exact
    #    real proof (rather than the dummy-but-valid-shaped points the full
    #    benchmark uses by default when no proof_json/public_json is set).
    if args.send_tx:
        accounts = load_accounts(args.accounts)
        sender = accounts[0]

        nonce = w3.eth.get_transaction_count(sender["address"], "pending")
        gas = int(w3.eth.estimate_gas({"to": verifier_address, "data": calldata_hex}) * 1.15)

        tx = {
            "chainId": chain_id,
            "nonce": nonce,
            "to": verifier_address,
            "value": 0,
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
            "data": calldata_hex,
        }
        signed = w3.eth.account.sign_transaction(tx, sender["private_key"])
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        print(f"\ntx {tx_hash.hex()} status={receipt.status} gasUsed={receipt.gasUsed}")


if __name__ == "__main__":
    main()
