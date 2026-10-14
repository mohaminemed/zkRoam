#!/usr/bin/env python3
"""
zkroam_workload.py

Custom transaction WORKLOAD for the Besu QBFT benchmark harness
(besu_benchmark.py), modeling the on-chain leg of the zkRoam CDR circuit
+ SnarkPack aggregation pipeline (snarkpack_aggregation/).

WHAT THIS MODELS
------------------------------------------------------------------
The Rust binary already measures the OFF-CHAIN cost of a roaming
settlement: generating `nproofs` Groth16 CDR proofs, then aggregating
them with SnarkPack (output/experiment_log_<n>_<run>.json).

This script models what happens next: getting a settlement result
on-chain. It benchmarks two competing strategies:

  * "individual" - post each of the `nproofs` Groth16 CDR proofs as
    its own transaction (no aggregation).
  * "aggregate"  - do the SnarkPack aggregation off-chain and post ONE
    transaction per VMNO carrying the aggregate attestation.

    individual pipeline = max(proof_gen_i) + individual confirm duration
    aggregate  pipeline = max(proof_gen_i) + aggregation_time
                          + aggregation_verify_time (+ serialize, if logged)
                          + aggregate confirm duration

WORKLOAD SIZE: nproofs (per-VMNO) vs num_vmnos (total workload)
------------------------------------------------------------------
    individual leg -> nproofs * num_vmnos total transactions
    aggregate  leg -> num_vmnos total transactions

OUTPUT SCHEMA
------------------------------------------------------------------
The sweep summary CSV always has the columns listed in
SWEEP_SUMMARY_FIELDS (same order as the reference output). Columns that
do not apply to the selected mode, or that the off-chain log does not
provide, are left empty.

  offchain_proof_ms_total            = sum of per-proof times (one VMNO)
  offchain_proof_work_ms_all_vmnos   = offchain_proof_ms_total * num_vmnos
  offchain_aggregate_work_ms_all_vmnos
                                     = (agg + agg_verify [+ serialize]) * num_vmnos
  *_latency_p50_s / *_latency_p99_s  = per-tx latency from the tx's OWN submission
                                       to the block that mined it (block time), so
                                       time spent submitting the rest of the batch
                                       and receipt-poller delay are NOT counted
  aggregate_latency_s                = p50 of that per-tx latency
  aggregate_first_tx_latency_s       = confirmation latency of the first tx

CONFIG FILE
------------------------------------------------------------------
All configuration lives in a YAML file (default: config.yml, override
with --config path/to/file.yml).

COMMIT LEG (per-session commit(sid, h_cdr))
------------------------------------------------------------------
    commit leg     -> nproofs * num_vmnos total transactions (one per session)
    e2e ZK-individual per session = commit + verifyProof gas
    e2e ZK-aggregate  per session = commit + anchor gas / nproofs
Needs contracts.deployed_contracts to hold a "cdr_commit" entry (see
deploy_cdr_commit.py); otherwise calldata + COMMIT_EXEC_COST is used.
Disable with workload.include_commit: false.

ATTESTERS (aggregate leg)
------------------------------------------------------------------
    workload.num_attesters (default 4): number of authorized verifiers that
    sign each aggregate attestation. relayAggregateProof() is called with
    num_attesters (signer, signature) pairs, so the anchor tx pays for
    num_attesters ecrecovers + attestation writes. Attesters are
    accounts[contracts.verifier_key_index : +num_attesters]; any that are not yet
    authorized on the anchor are added once at start-up (addVerifier, sent by the
    anchor's owner, which must be one of the funded accounts).
    workload.threshold (default null): if set, setThreshold(t) is applied at
    start-up; null leaves the on-chain threshold untouched.

IMPORTANT ASSUMPTIONS
------------------------------------------------------------------
1. Without deployed contracts only CALLDATA cost is modeled (EIP-2028);
   with contracts.deployed_contracts pointing at a deploy_verifiers.py
   output, real calls are made and real gasUsed is read from receipts.
2. SnarkPack aggregate-proof size is estimated analytically unless the
   Rust log provides "aggregate_proof_bytes".
3. "individual" mode posts a full compressed Groth16 proof (192 bytes)
   per transaction.
------------------------------------------------------------------
"""

import argparse
import csv
import glob
import json
import math
import os
import secrets
import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import yaml

from besu_benchmark import (  # the pasted script, save it as besu_benchmark.py
    ResourceMonitor,
    build_rpc_endpoints,
    connect_web3,
    load_accounts,
    assign_accounts,
    load_nonces,
    submit_all,
    poll_all_receipts,
    compute_statistics,
    compute_rpc_statistics,
    percentile,
    load_json,
)

from web3 import Web3
from eth_abi import encode as eth_abi_encode
from eth_keys import keys as eth_keys


# ============================================================
# Sweep-summary schema (order matters: matches the reference CSV)
# ============================================================

SWEEP_SUMMARY_FIELDS = [
    "nproofs_per_vmno", "num_vmnos", "run",
    "offchain_source",
    "offchain_proof_ms_max", "offchain_proof_ms_avg", "offchain_proof_ms_total",
    "offchain_param_ms", "offchain_single_verify_ms",
    "offchain_agg_ms", "offchain_agg_serialize_ms", "offchain_agg_verify_ms",
    "offchain_peak_mb", "offchain_total_experiment_ms",
    "offchain_proof_work_ms_all_vmnos", "offchain_aggregate_work_ms_all_vmnos",
    "individual_total_tx", "individual_bytes_per_tx",
    "individual_gas_limit_per_tx", "individual_gas_used_per_tx",
    "individual_total_gas_used", "individual_contract_called",
    "individual_confirmed", "individual_success_rate_pct",
    "individual_submit_throughput_tx_s", "individual_confirm_throughput_tx_s",
    "individual_latency_p50_s", "individual_latency_p99_s",
    "individual_confirm_duration_s", "individual_pipeline_latency_s",
    "aggregate_total_tx", "aggregate_proof_bytes", "aggregate_proof_bytes_is_measured",
    "aggregate_gas_limit_per_tx", "aggregate_gas_used_per_tx",
    "aggregate_total_gas_used", "aggregate_contract_called",
    "aggregate_confirmed", "aggregate_success_rate_pct",
    "aggregate_submit_throughput_tx_s", "aggregate_confirm_throughput_tx_s",
    "aggregate_latency_p50_s", "aggregate_latency_p99_s",
    "aggregate_latency_s", "aggregate_first_tx_latency_s",
    "aggregate_confirm_duration_s", "aggregate_pipeline_latency_s",
    "gas_savings_pct", "pipeline_speedup_x",
    # --- per-session commitment tx (sid, h_cdr) + end-to-end accounting ---
    "commit_total_tx", "commit_bytes_per_tx",
    "commit_gas_limit_per_tx", "commit_gas_used_per_tx",
    "commit_total_gas_used", "commit_contract_called",
    "commit_confirmed", "commit_success_rate_pct",
    "commit_submit_throughput_tx_s", "commit_confirm_throughput_tx_s",
    "commit_latency_p50_s", "commit_latency_p99_s", "commit_confirm_duration_s",
    "e2e_individual_gas_per_session", "e2e_aggregate_gas_per_session",
    "e2e_individual_total_gas", "e2e_aggregate_total_gas",
    "e2e_gas_savings_pct", "commit_share_of_aggregate_e2e_pct",
    # --- aggregate attestation set ---
    "aggregate_num_attesters", "anchor_threshold",
]


# ============================================================
# Deployed-contract wiring (see deploy_verifiers.py)
# ============================================================

CDR_VERIFIER_ABI = [{
    "type": "function", "name": "verifyProof", "stateMutability": "view",
    "inputs": [
        {"name": "_pA", "type": "uint256[2]"},
        {"name": "_pB", "type": "uint256[2][2]"},
        {"name": "_pC", "type": "uint256[2]"},
        {"name": "_pubSignals", "type": "uint256[5]"},
    ],
    "outputs": [{"name": "", "type": "bool"}],
}]

CDR_COMMIT_ABI = [{
    "type": "function", "name": "commit", "stateMutability": "nonpayable",
    "inputs": [
        {"name": "sid", "type": "bytes32"},
        {"name": "hCdr", "type": "bytes32"},
    ],
    "outputs": [],
}]

AGGREGATE_ANCHOR_ABI = [{
    "type": "function", "name": "anchorAggregateProof", "stateMutability": "nonpayable",
    "inputs": [
        {"name": "settlementId", "type": "bytes32"},
        {"name": "commitment", "type": "bytes32"},
        {"name": "nproofs", "type": "uint64"},
    ],
    "outputs": [],
}, {
    "type": "function", "name": "relayAggregateProof", "stateMutability": "nonpayable",
    "inputs": [
        {"name": "settlementId", "type": "bytes32"},
        {"name": "commitment", "type": "bytes32"},
        {"name": "nproofs", "type": "uint64"},
        {"name": "signers", "type": "address[]"},
        {"name": "signatures", "type": "bytes[]"},
    ],
    "outputs": [],
}]


# Admin / view functions used to provision the attester set.
AGGREGATE_ANCHOR_ABI += [
    {"type": "function", "name": "owner", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "threshold", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "verifierCount", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "isVerifier", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "addVerifier", "stateMutability": "nonpayable",
     "inputs": [{"name": "v", "type": "address"}], "outputs": []},
    {"type": "function", "name": "setThreshold", "stateMutability": "nonpayable",
     "inputs": [{"name": "t", "type": "uint256"}], "outputs": []},
]

# ============================================================
# EIP-712 attestation signing (matches SnarkPackAggregateAnchor.sol)
# ============================================================

EIP712_DOMAIN_TYPEHASH = Web3.keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
ATTEST_TYPEHASH = Web3.keccak(
    text="AggregateAttestation(bytes32 settlementId,bytes32 commitment,uint64 nproofs)"
)


def anchor_domain_separator(chain_id: int, verifying_contract: str) -> bytes:
    return Web3.keccak(
        eth_abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                EIP712_DOMAIN_TYPEHASH,
                Web3.keccak(text="SnarkPackAggregateAnchor"),
                Web3.keccak(text="1"),
                chain_id,
                Web3.to_checksum_address(verifying_contract),
            ],
        )
    )


def attestation_digest(domain_separator: bytes, settlement_id: bytes,
                        commitment: bytes, nproofs: int) -> bytes:
    struct_hash = Web3.keccak(
        eth_abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint64"],
            [ATTEST_TYPEHASH, settlement_id, commitment, nproofs],
        )
    )
    return Web3.keccak(b"\x19\x01" + domain_separator + struct_hash)


def sign_attestation(digest: bytes, private_key_hex: str) -> bytes:
    """
    Raw secp256k1 sign of an already-fully-formed EIP-712 digest (no
    extra 'Ethereum Signed Message' prefix). eth_keys emits low-s
    signatures, satisfying the contract's malleability check.
    """
    pk = eth_keys.PrivateKey(bytes.fromhex(private_key_hex.replace("0x", "")))
    sig = pk.sign_msg_hash(digest)
    v = sig.v + 27
    return sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([v])


def build_relay_calldata(w3, settlement_id: bytes, commitment: bytes,
                          nproofs: int, signer_address: str, signature: bytes):
    contract = w3.eth.contract(abi=AGGREGATE_ANCHOR_ABI)
    return contract.encode_abi(
        abi_element_identifier="relayAggregateProof",
        args=[
            settlement_id, commitment, nproofs,
            [Web3.to_checksum_address(signer_address)],
            [signature],
        ],
    )

def build_relay_calldata_multi(w3, settlement_id: bytes, commitment: bytes,
                                nproofs: int, signer_addresses, signatures):
    """relayAggregateProof with several attesters. The contract requires the
    signers array to be strictly ascending by address (see pick_attesters)."""
    contract = w3.eth.contract(abi=AGGREGATE_ANCHOR_ABI)
    return contract.encode_abi(
        abi_element_identifier="relayAggregateProof",
        args=[
            settlement_id, commitment, nproofs,
            [Web3.to_checksum_address(a) for a in signer_addresses],
            list(signatures),
        ],
    )


def pick_attesters(accounts, key_index, n):
    """accounts[key_index : key_index+n], sorted ascending by address (the
    anchor rejects unsorted signer lists)."""
    if n < 1:
        raise ValueError(f"workload.num_attesters must be >= 1, got {n}")
    if key_index + n > len(accounts):
        raise RuntimeError(
            f"num_attesters={n} from verifier_key_index={key_index} needs "
            f"{key_index + n} funded accounts, only {len(accounts)} available"
        )
    return sorted(accounts[key_index:key_index + n],
                  key=lambda a: int(a["address"], 16))


def _send_admin_tx(w3, chain_id, acct, to, data, gas=150_000):
    addr = Web3.to_checksum_address(acct["address"])
    tx = {
        "chainId": chain_id,
        "nonce": w3.eth.get_transaction_count(addr, "pending"),
        "to": to, "value": 0, "data": data, "gas": gas,
        "gasPrice": w3.eth.gas_price,
    }
    signed = w3.eth.account.sign_transaction(tx, acct["private_key"])
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    if rcpt["status"] != 1:
        raise RuntimeError(f"admin tx reverted: {h.hex()}")


def ensure_attesters(w3, chain_id, anchor_address, accounts, attesters, threshold=None):
    """Idempotent provisioning: authorize every attester on the anchor and
    optionally set the threshold. Returns the on-chain threshold."""
    anchor = w3.eth.contract(address=anchor_address, abi=AGGREGATE_ANCHOR_ABI)
    missing = [a for a in attesters
               if not anchor.functions.isVerifier(
                   Web3.to_checksum_address(a["address"])).call()]
    need_threshold = (threshold is not None
                      and anchor.functions.threshold().call() != threshold)

    if missing or need_threshold:
        owner = anchor.functions.owner().call()
        owner_acct = next(
            (a for a in accounts
             if Web3.to_checksum_address(a["address"]) == owner), None)
        if owner_acct is None:
            raise RuntimeError(
                f"anchor owner {owner} is not among the funded accounts; cannot "
                f"authorize {len(missing)} attester(s) / set threshold. Redeploy "
                f"the anchor from an account in accounts.json."
            )
        for a in missing:
            print(f"  addVerifier({a['address']})")
            _send_admin_tx(w3, chain_id, owner_acct, anchor_address,
                           anchor.encode_abi("addVerifier",
                                             args=[Web3.to_checksum_address(a["address"])]))
        if need_threshold:
            print(f"  setThreshold({threshold})")
            _send_admin_tx(w3, chain_id, owner_acct, anchor_address,
                           anchor.encode_abi("setThreshold", args=[threshold]))

    t = anchor.functions.threshold().call()
    print(f"anchor attesters: {len(attesters)} signing, "
          f"{anchor.functions.verifierCount().call()} authorized, threshold={t}")
    return t


# Known-valid BN254 G1/G2 points lifted from the verifier's own vk
# constants, so the precompile calls run to completion (cryptographically
# invalid proof, but the tx succeeds): honest for gas/latency benchmarking.
DUMMY_PA = (
    2398337629181014763546313145651356636255150315462676709288079856466236234747,
    13840928493832268677418897163492247680112434824843838084394385339941026710099,
)
DUMMY_PB = (
    (11559732032986387107991004021392285783925812861821192530917403151452391805634,
     10857046999023057135944570762232829481370756359578518086990519993285655852781),
    (4082367875863433681332203403145435568316851327593401208105741076214120093531,
     8495653923123431417604973247489272438418190587263600148770280649306958101930),
)
DUMMY_PC = (
    16713112450031421725042549811723212074162717794601377327847381683977378237164,
    9317295738394475329924888113202461969087156787908295676667549706052785058782,
)
DUMMY_PUBLIC_SIGNALS = (1, 2, 3, 4, 5)


def load_deployed_contracts(path):
    if not path or not os.path.exists(path):
        return None
    return load_json(path)


def load_snarkjs_proof(proof_path, public_path):
    """Parse a real snarkjs proof.json / public.json pair, if supplied."""
    proof = load_json(proof_path)
    public_signals = tuple(int(x) for x in load_json(public_path))

    pA = (int(proof["pi_a"][0]), int(proof["pi_a"][1]))
    # snarkjs G2 coordinates are [c0, c1]; Solidity's pB expects (c1, c0).
    pB = (
        (int(proof["pi_b"][0][1]), int(proof["pi_b"][0][0])),
        (int(proof["pi_b"][1][1]), int(proof["pi_b"][1][0])),
    )
    pC = (int(proof["pi_c"][0]), int(proof["pi_c"][1]))

    return pA, pB, pC, public_signals


def build_verify_proof_calldata(w3, pA=None, pB=None, pC=None, public_signals=None):
    pA = pA or DUMMY_PA
    pB = pB or DUMMY_PB
    pC = pC or DUMMY_PC
    public_signals = public_signals or DUMMY_PUBLIC_SIGNALS

    contract = w3.eth.contract(abi=CDR_VERIFIER_ABI)
    return contract.encode_abi(
        abi_element_identifier="verifyProof",
        args=[list(pA), [list(pB[0]), list(pB[1])], list(pC), list(public_signals)],
    )


def build_commit_calldata(w3, sid: bytes, h_cdr: bytes):
    """commit(bytes32 sid, bytes32 hCdr) calldata: 4B selector + 64B = 68B."""
    contract = w3.eth.contract(abi=CDR_COMMIT_ABI)
    return contract.encode_abi(
        abi_element_identifier="commit", args=[sid, h_cdr],
    )


def build_anchor_calldata(w3, settlement_id: bytes, commitment: bytes, nproofs: int):
    """Direct anchorAggregateProof(onlyVerifier) calldata - kept for
    smoke-testing; the sweep driver uses build_relay_calldata()."""
    contract = w3.eth.contract(abi=AGGREGATE_ANCHOR_ABI)
    return contract.encode_abi(
        abi_element_identifier="anchorAggregateProof",
        args=[settlement_id, commitment, nproofs],
    )


def fetch_gas_used(transactions, web3s):
    """Post-hoc fetch of real gasUsed for confirmed txs."""
    used = []
    for t in transactions:
        if t["status"] != "confirmed" or not t["tx_hash"]:
            continue
        try:
            receipt = web3s[t["rpc_index"]].eth.get_transaction_receipt(t["tx_hash"])
            t["gas_used"] = receipt["gasUsed"]
            used.append(receipt["gasUsed"])
        except Exception:
            t["gas_used"] = None
    return used


def block_timestamp(w3, block_number):
    """Block timestamp via raw RPC. web3.py's get_block() validates extraData
    length and raises ExtraDataLengthError on QBFT/IBFT chains (the header
    carries validator set + seals), so it must not be used here."""
    res = w3.provider.make_request("eth_getBlockByNumber", [hex(block_number), False])
    if res.get("error") or not res.get("result"):
        return None
    return int(res["result"]["timestamp"], 16)


def apply_block_time_latency(transactions, web3s, method="block_time"):
    """
    Per-tx latency = (timestamp of the block that mined the tx) - (that
    tx's own submit_ts).

    The receipt poller only starts after submit_all() returns, so
    confirm_ts - submit_ts also contains the time spent submitting the
    rest of the batch plus the poll sweep. Block time avoids that.

    Safeguards:
      * block timestamps are whole seconds -> clamp at >= 0
      * never exceed the poll-based latency (the true latency cannot be
        larger), which also bounds the error from client/node clock skew
      * if a block cannot be fetched, fall back to the poll-based value

    Sets t["latency_s"], t["poll_latency_s"], t["block_ts"] on every tx and
    returns {"p50", "p99", "mean", "n"} over confirmed txs.
    """
    block_ts_cache = {}

    for t in transactions:
        t["latency_s"] = None
        t["poll_latency_s"] = None
        t["block_ts"] = None

        if t["submit_ts"] is None or t["confirm_ts"] is None:
            continue

        poll_lat = t["confirm_ts"] - t["submit_ts"]
        t["poll_latency_s"] = poll_lat
        lat = poll_lat

        if (method == "block_time" and t["status"] == "confirmed"
                and t["block_number"] is not None):
            bn = t["block_number"]
            if bn not in block_ts_cache:
                try:
                    block_ts_cache[bn] = block_timestamp(web3s[t["rpc_index"]], bn)
                except Exception:
                    block_ts_cache[bn] = None
            bts = block_ts_cache[bn]
            if bts is not None:
                t["block_ts"] = bts
                lat = min(poll_lat, max(0.0, bts - t["submit_ts"]))

        t["latency_s"] = lat

    if method == "block_time":
        missing = sum(1 for t in transactions
                      if t["status"] == "confirmed" and t["block_ts"] is None)
        if missing:
            print(f"  WARNING: block timestamp unavailable for {missing} confirmed "
                  f"txs - they use poll-based latency instead.")

    vals = sorted(t["latency_s"] for t in transactions
                  if t["status"] == "confirmed" and t["latency_s"] is not None)
    if not vals:
        return {"p50": None, "p99": None, "mean": None, "n": 0}

    def pct(q):
        k = (len(vals) - 1) * q
        lo, hi = math.floor(k), math.ceil(k)
        return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)

    return {"p50": pct(0.50), "p99": pct(0.99),
            "mean": sum(vals) / len(vals), "n": len(vals)}


def wait_for_pool_drain(accounts, assignment, web3s, timeout_s=900.0, poll_s=2.0):
    """
    Block until no workload account has pending-but-unmined txs, i.e.
    eth_getTransactionCount(addr, "pending") == (addr, "latest") for all.
    Returns (drained: bool, waited_s, remaining_backlog).
    """
    def backlog(acc):
        w3 = web3s[assignment[acc["index"]]]
        try:
            return (w3.eth.get_transaction_count(acc["address"], "pending")
                    - w3.eth.get_transaction_count(acc["address"], "latest"))
        except Exception:
            return 0

    start = time.time()
    remaining = 0
    while True:
        with ThreadPoolExecutor(max_workers=16) as ex:
            remaining = sum(ex.map(backlog, accounts))
        if remaining == 0:
            return True, time.time() - start, 0
        if time.time() - start > timeout_s:
            return False, time.time() - start, remaining
        time.sleep(poll_s)


def settle_network(label, accounts, assignment, web3s, timeout_s, resource_monitor=None):
    """
    Called between legs/runs so one leg's backlog (e.g. a 16k-tx individual
    leg at 500 tx/s) cannot congest the next one: wait for the pool to
    drain, then re-read nonces from the chain instead of assuming every
    submitted tx was mined.
    """
    if resource_monitor is not None:
        resource_monitor.set_phase(f"settle:{label}")

    drained, waited, remaining = wait_for_pool_drain(
        accounts, assignment, web3s, timeout_s=timeout_s
    )
    if drained:
        print(f"  [settle:{label}] pool drained after {waited:.1f}s")
    else:
        print(f"  [settle:{label}] WARNING: {remaining} txs still pending after "
              f"{waited:.0f}s - next leg may be affected (raise "
              f"execution.drain_timeout_s or lower execution.rate)")

    nonces = load_nonces(accounts, assignment, web3s)

    if resource_monitor is not None:
        resource_monitor.set_phase("idle")
    return nonces


# ============================================================
# BLS12-381 / SnarkPack size constants
# ============================================================

G1_COMPRESSED_BYTES = 48
G2_COMPRESSED_BYTES = 96
GT_BYTES = 576
FR_BYTES = 32

GROTH16_PROOF_BYTES = (2 * G1_COMPRESSED_BYTES) + G2_COMPRESSED_BYTES  # 192B

# EIP-2028 calldata gas
GAS_PER_ZERO_BYTE = 4
GAS_PER_NONZERO_BYTE = 16
BASE_TX_GAS = 21_000
EXEC_COST = 206000
# Analytic execution cost of CDRCommitment.commit() (fallback when no contract
# is deployed): SSTORE 0->nonzero 22,100 (incl. cold access) + LOG2 1,381
# + dispatch/ABI/checks ~500, rounded up.
COMMIT_EXEC_COST = 24_000


# ============================================================
# Reference off-chain numbers (fallback only)
# ============================================================

REFERENCE_OFFCHAIN_STATS = {
    8:    {"proof_ms": 30.33, "agg_ms": 42.07,   "agg_verify_ms": 12.67, "peak_mb": 19.7},
    64:   {"proof_ms": 26.38, "agg_ms": 110.86,  "agg_verify_ms": 12.49, "peak_mb": 29.9},
    128:  {"proof_ms": 25.88, "agg_ms": 180.76,  "agg_verify_ms": 11.44, "peak_mb": 30.5},
    256:  {"proof_ms": 26.65, "agg_ms": 296.91,  "agg_verify_ms": 12.08, "peak_mb": 31.3},
    512:  {"proof_ms": 26.10, "agg_ms": 527.35,  "agg_verify_ms": 12.19, "peak_mb": 32.7},
    1024: {"proof_ms": 25.96, "agg_ms": 1323.86, "agg_verify_ms": 14.11, "peak_mb": 36.1},
    2048: {"proof_ms": 25.64, "agg_ms": 3185.76, "agg_verify_ms": 14.41, "peak_mb": 281.1},
}

DEFAULT_SWEEP = [8]  # , 64, 128, 256, 512, 1024, 2048]


# ============================================================
# Off-chain log loading (the Rust output/ directory)
# ============================================================

def _first_key(d, keys):
    """First present, non-null value among candidate JSON keys, else None."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


# Candidate key names in the Rust ExperimentLog JSON. Adjust here if your
# struct uses different field names (missing ones simply stay empty).
PARAM_KEYS = ["param_time_ms", "params_time_ms", "param_gen_time_ms",
              "setup_time_ms", "parameter_time_ms"]
SINGLE_VERIFY_KEYS = ["single_verify_time_ms", "single_proof_verify_time_ms",
                      "verify_time_ms", "individual_verify_time_ms"]
AGG_SERIALIZE_KEYS = ["aggregation_serialize_time_ms", "aggregate_serialize_time_ms",
                      "serialize_time_ms", "aggregation_serialization_time_ms"]
TOTAL_EXPERIMENT_KEYS = ["total_experiment_time_ms", "total_time_ms"]


def load_offchain_log(offchain_dir, nproofs, run):
    """
    Look for output/experiment_log_<nproofs>_<run>.json produced by the
    Rust binary. Falls back to REFERENCE_OFFCHAIN_STATS if missing
    (in which case param/single-verify/serialize/total are unknown).
    """

    record = {
        "source": "reference_fallback",
        "aggregate_proof_bytes_measured": None,
        "proof_ms_total": None,
        "param_ms": None,
        "single_verify_ms": None,
        "agg_serialize_ms": None,
        "total_experiment_ms": None,
    }

    if offchain_dir:
        path = os.path.join(offchain_dir, f"experiment_log_{nproofs}_{run}.json")
        if os.path.exists(path):
            d = load_json(path)
            proof_times = [p["time_ms"] for p in d["proofs"]]
            record.update(
                {
                    "source": path,
                    "proof_ms_max": max(proof_times),
                    "proof_ms_avg": sum(proof_times) / len(proof_times),
                    "proof_ms_total": sum(proof_times),
                    "param_ms": _first_key(d, PARAM_KEYS),
                    "single_verify_ms": _first_key(d, SINGLE_VERIFY_KEYS),
                    "agg_ms": d["aggregation_time_ms"],
                    "agg_serialize_ms": _first_key(d, AGG_SERIALIZE_KEYS),
                    "agg_verify_ms": d["aggregation_verify_time_ms"],
                    "peak_mb": d["peak_memory_bytes"] / (1024 * 1024),
                    "total_experiment_ms": _first_key(d, TOTAL_EXPERIMENT_KEYS),
                    "aggregate_proof_bytes_measured": d.get("aggregate_proof_bytes"),
                }
            )
            return record

    ref = REFERENCE_OFFCHAIN_STATS.get(nproofs)
    if ref is None:
        nearest = min(REFERENCE_OFFCHAIN_STATS, key=lambda k: abs(k - nproofs))
        ref = REFERENCE_OFFCHAIN_STATS[nearest]

    record.update(
        {
            "proof_ms_max": ref["proof_ms"],
            "proof_ms_avg": ref["proof_ms"],
            "proof_ms_total": ref["proof_ms"] * nproofs,
            "agg_ms": ref["agg_ms"],
            "agg_verify_ms": ref["agg_verify_ms"],
            "peak_mb": ref["peak_mb"],
        }
    )

    return record


# ============================================================
# Proof / calldata sizing
# ============================================================

def estimate_aggregate_proof_bytes(nproofs, measured_bytes=None):
    """
    Analytic estimate of SnarkPack aggregate-proof size, used only when
    the Rust side hasn't reported a real serialized byte count.
    """

    if measured_bytes:
        return measured_bytes

    rounds = max(1, math.ceil(math.log2(max(nproofs, 2))))

    fixed = (3 * GT_BYTES) + G1_COMPRESSED_BYTES
    per_round = (4 * GT_BYTES) + (4 * G1_COMPRESSED_BYTES)
    finalize = (2 * G1_COMPRESSED_BYTES) + G2_COMPRESSED_BYTES + FR_BYTES

    return fixed + (rounds * per_round) + finalize


def calldata_gas_cost(payload: bytes):
    nonzero = sum(1 for b in payload if b != 0)
    zero = len(payload) - nonzero
    return BASE_TX_GAS + (nonzero * GAS_PER_NONZERO_BYTE) + (zero * GAS_PER_ZERO_BYTE)


def make_calldata(n_bytes):
    """Random bytes stand in for serialized curve points/field elements."""
    return secrets.token_bytes(n_bytes)


# ============================================================
# Transaction preparation (zkRoam variant - adds calldata + variable gas)
# ============================================================

def prepare_zkroam_transactions(
    accounts,
    assignment,
    nonces,
    web3s,
    num_tx,
    chain_id,
    calldata,
    gas_per_tx: int,
    to_address=None,
):
    """
    Same shape as benchmark.py's prepare_transactions(), but every tx
    carries calldata as `data` and uses an explicit gas limit.

    `calldata` is either a single `bytes` object reused for all `num_tx`
    transactions (individual leg) or a list/tuple of `num_tx` distinct
    `bytes` objects (aggregate leg: each VMNO needs its own settlementId).

    `num_tx` is the TOTAL transaction count for this leg, already
    resolved by the caller (nproofs * num_vmnos or num_vmnos).
    """

    if not accounts:
        raise RuntimeError("No accounts available.")

    per_tx_calldata = isinstance(calldata, (list, tuple))
    if per_tx_calldata and len(calldata) != num_tx:
        raise ValueError(
            f"calldata list has {len(calldata)} entries but num_tx={num_tx}"
        )

    transactions = []
    next_nonce = dict(nonces)
    single_data_hex = None if per_tx_calldata else Web3.to_hex(calldata)

    for tx_index in range(num_tx):

        account = accounts[tx_index % len(accounts)]
        account_index = account["index"]
        rpc_index = assignment[account_index]
        w3 = web3s[rpc_index]

        nonce = next_nonce[account_index]
        next_nonce[account_index] += 1

        target = to_address or account["address"]

        data_hex = single_data_hex or Web3.to_hex(calldata[tx_index])

        tx = {
            "chainId": chain_id,
            "nonce": nonce,
            "to": target,
            "value": 0,
            "gas": gas_per_tx,
            "gasPrice": w3.eth.gas_price,
            "data": data_hex,
        }

        signed = w3.eth.account.sign_transaction(tx, account["private_key"])

        transactions.append(
            {
                "tx_index": tx_index,
                "account_index": account_index,
                "sender": account["address"],
                "rpc_index": rpc_index,
                "raw": signed.raw_transaction,
                "nonce": nonce,
                "tx_hash": None,
                "submit_ts": None,
                "confirm_ts": None,
                "block_number": None,
                "status": "prepared",
                "error": None,
            }
        )

    return transactions


# ============================================================
# One benchmark leg: submit + confirm a batch, return on-chain stats
# ============================================================

def run_onchain_leg(
    label,
    accounts,
    assignment,
    nonces,
    web3s,
    endpoints,
    chain_id,
    num_tx,
    calldata_bytes,
    gas_per_tx,
    rate,
    workers,
    receipt_workers,
    receipt_timeout,
    poll_interval,
    resource_monitor=None,
    to_address=None,
    accounts_override=None,
    receipt_timeout_per_tx=0.0,
):
    # Large legs (e.g. 16k individual txs) can legitimately need far longer
    # than a flat timeout: scale it with the leg size.
    receipt_timeout = max(receipt_timeout, num_tx * receipt_timeout_per_tx)
    print()
    calldata_size_desc = (
        f"{len(calldata_bytes[0])}B/tx x {len(calldata_bytes)} distinct payloads"
        if isinstance(calldata_bytes, (list, tuple))
        else f"{len(calldata_bytes)}B calldata"
    )
    print(f">>> on-chain leg: {label} "
          f"({num_tx} tx total, {calldata_size_desc}, "
          f"gas/tx={gas_per_tx}, to={to_address or '(self)'})")

    transactions = prepare_zkroam_transactions(
        accounts=accounts_override or accounts,
        assignment=assignment,
        nonces=nonces,
        web3s=web3s,
        num_tx=num_tx,
        chain_id=chain_id,
        calldata=calldata_bytes,
        gas_per_tx=gas_per_tx,
        to_address=to_address,
    )

    if resource_monitor is not None:
        resource_monitor.set_phase(f"{label}:submission")

    submit_all(transactions, web3s, target_rate=rate, workers=workers)

    if resource_monitor is not None:
        resource_monitor.set_phase(f"{label}:confirmation")

    poll_all_receipts(
        transactions, web3s,
        workers=receipt_workers,
        timeout=receipt_timeout,
        poll_interval=poll_interval,
    )

    if resource_monitor is not None:
        resource_monitor.set_phase("idle")

    stats = compute_statistics(transactions)
    rpc_stats = compute_rpc_statistics(transactions, endpoints)

    return transactions, stats, rpc_stats


# ============================================================
# YAML config loading
# ============================================================

DEFAULT_CONFIG = {
    "experiment": {
        "name": "zkroam_benchmark",
    },
    "network": {
        "topology": "networkFiles/topology.json",
        "accounts": "accounts/accounts.json",
    },
    "sweep": {
        "nproofs": list(DEFAULT_SWEEP),
        "runs": 1,
        "mode": "both",  # individual | aggregate | both
    },
    "workload": {
        # individual leg total tx = nproofs * num_vmnos
        # aggregate  leg total tx = num_vmnos
        "num_vmnos": 1000,
        # one commit(sid, h_cdr) tx per SESSION = nproofs * num_vmnos txs,
        # shared by both ZK variants (individual and aggregate)
        "include_commit": True,
        # number of authorized verifiers signing each aggregate attestation
        # (relayAggregateProof carries this many signatures)
        "num_attesters": 4,
        # anchor finalization threshold; null = leave the on-chain value as is
        "threshold": None,
    },
    "offchain": {
        "logs_dir": "snarkpack_aggregation/output",
    },
    "contracts": {
        "verifier_address": None,
        "deployed_contracts": "deployed_contracts.json",
        "proof_json": None,
        "public_json": None,
        "verifier_key_index": 0,
    },
    "execution": {
        "rate": 500.0,
        "workers": 16,
        "receipt_workers": 16,
        "receipt_timeout": 120.0,
        "poll_interval": 0.25,
        # "block_time": latency = mining block timestamp - tx's own submit_ts
        # "poll":       latency = receipt-seen time - submit_ts (old behaviour)
        "latency_method": "block_time",
        # receipt timeout for a leg = max(receipt_timeout, num_tx * this)
        "receipt_timeout_per_tx_s": 0.05,
        # max time to wait for the txpool to drain between legs/runs
        "drain_timeout_s": 900.0,
    },
    "monitoring": {
        "enabled": True,
        "interval": 1.0,
    },
    "output": {
        "out_dir": "results/zkroam",
    },
}


def _deep_merge(base, override):
    merged = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_config(path):
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Copy config.example.yml to {path} and edit it, or pass "
            f"--config /path/to/the.yml."
        )

    with open(path) as f:
        user_cfg = yaml.safe_load(f) or {}

    cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)

    args = SimpleNamespace(
        experiment_name=cfg["experiment"]["name"],
        topology=cfg["network"]["topology"],
        accounts=cfg["network"]["accounts"],
        sweep=cfg["sweep"]["nproofs"],
        runs=int(cfg["sweep"]["runs"]),
        mode=cfg["sweep"]["mode"],
        num_vmnos=int(cfg["workload"]["num_vmnos"]),
        include_commit=bool(cfg["workload"]["include_commit"]),
        num_attesters=int(cfg["workload"]["num_attesters"]),
        threshold=(None if cfg["workload"]["threshold"] is None
                   else int(cfg["workload"]["threshold"])),
        offchain_logs_dir=cfg["offchain"]["logs_dir"],
        verifier_address=cfg["contracts"]["verifier_address"],
        deployed_contracts=cfg["contracts"]["deployed_contracts"],
        proof_json=cfg["contracts"]["proof_json"],
        public_json=cfg["contracts"]["public_json"],
        verifier_key_index=int(cfg["contracts"]["verifier_key_index"]),
        rate=float(cfg["execution"]["rate"]),
        workers=int(cfg["execution"]["workers"]),
        receipt_workers=int(cfg["execution"]["receipt_workers"]),
        receipt_timeout=float(cfg["execution"]["receipt_timeout"]),
        poll_interval=float(cfg["execution"]["poll_interval"]),
        latency_method=str(cfg["execution"]["latency_method"]),
        receipt_timeout_per_tx=float(cfg["execution"]["receipt_timeout_per_tx_s"]),
        drain_timeout_s=float(cfg["execution"]["drain_timeout_s"]),
        monitor_resources=bool(cfg["monitoring"]["enabled"]),
        monitor_interval=float(cfg["monitoring"]["interval"]),
        out_dir=cfg["output"]["out_dir"],
    )

    if isinstance(args.sweep, str):
        args.sweep = [int(x) for x in args.sweep.split(",") if str(x).strip()]
    else:
        args.sweep = [int(x) for x in args.sweep]

    if args.latency_method not in ("block_time", "poll"):
        raise ValueError(
            f"execution.latency_method must be block_time or poll, got {args.latency_method!r}"
        )

    if args.num_attesters < 1 or args.num_attesters > 50:
        raise ValueError(
            f"workload.num_attesters must be in [1, 50] (contract MAX_BATCH_SIZE), "
            f"got {args.num_attesters}"
        )
    if args.threshold is not None and not (1 <= args.threshold <= args.num_attesters):
        raise ValueError(
            f"workload.threshold must be in [1, num_attesters={args.num_attesters}], "
            f"got {args.threshold}"
        )

    if args.mode not in ("individual", "aggregate", "both"):
        raise ValueError(
            f"sweep.mode must be one of individual/aggregate/both, got {args.mode!r}"
        )

    return args


# ============================================================
# Sweep driver
# ============================================================

def main():

    cli = argparse.ArgumentParser(
        description=(
            "zkRoam / SnarkPack on-chain workload for the Besu "
            "benchmark harness: individual-proof vs aggregate-proof "
            "settlement, swept over per-VMNO proof count. "
            "All other configuration lives in the YAML config file."
        )
    )
    cli.add_argument(
        "--config", default="config.yml",
        help="Path to the YAML config file (see config.example.yml).",
    )
    cli_args = cli.parse_args()

    args = load_config(cli_args.config)

    sweep = args.sweep
    os.makedirs(args.out_dir, exist_ok=True)

    resource_monitor = None
    if args.monitor_resources:
        resource_monitor = ResourceMonitor(args.monitor_interval)
        resource_monitor.start()
        resource_monitor.set_phase("setup:connecting")

    topology = load_json(args.topology)
    endpoints = build_rpc_endpoints(topology)
    web3s, chain_id = connect_web3(endpoints)

    if resource_monitor is not None:
        resource_monitor.set_phase("setup:loading_accounts")

    accounts = load_accounts(args.accounts)
    if not accounts:
        raise RuntimeError("No workload accounts found.")

    assignment = assign_accounts(accounts, endpoints)

    if resource_monitor is not None:
        resource_monitor.set_phase("setup:loading_nonces")

    nonces = load_nonces(accounts, assignment, web3s)

    if resource_monitor is not None:
        resource_monitor.set_phase("idle")

    deployed = load_deployed_contracts(args.deployed_contracts)

    cdr_verifier_address = None
    if args.verifier_address:
        cdr_verifier_address = Web3.to_checksum_address(args.verifier_address)
    elif deployed and deployed.get("cdr_verifier"):
        cdr_verifier_address = Web3.to_checksum_address(deployed["cdr_verifier"]["address"])

    anchor_address = None
    if deployed and deployed.get("aggregate_anchor"):
        anchor_address = Web3.to_checksum_address(deployed["aggregate_anchor"]["address"])

    commit_address = None
    if deployed and deployed.get("cdr_commit"):
        commit_address = Web3.to_checksum_address(deployed["cdr_commit"]["address"])
    if args.include_commit:
        if commit_address:
            print(f"commit leg     -> real commit(sid, h_cdr) calls on {commit_address}")
        else:
            print("commit leg     -> no deployed cdr_commit found, "
                  "falling back to calldata + analytic exec-cost estimate "
                  "(run deploy_cdr_commit.py for measured gasUsed)")

    real_proof = None
    if args.proof_json and args.public_json:
        real_proof = load_snarkjs_proof(args.proof_json, args.public_json)
        print(f"\nUsing real proof from {args.proof_json} / {args.public_json}")
    elif cdr_verifier_address:
        print(
            "\nNo contracts.proof_json/public_json given: 'individual' will call "
            "verifyProof() with known-valid-but-cryptographically-wrong curve "
            "points, so gas reflects a full precompile execution but the proof "
            "itself won't verify. Set real snarkjs files in the config for a "
            "true positive."
        )

    if cdr_verifier_address:
        print(f"individual leg -> real verifyProof() calls on {cdr_verifier_address}")
    else:
        print("individual leg -> no deployed cdr_verifier found, "
              "falling back to raw-calldata-cost estimate (no contract execution)")

    if anchor_address:
        print(f"aggregate leg  -> real relayAggregateProof() calls on {anchor_address}")
    else:
        print("aggregate leg  -> no deployed aggregate_anchor found, "
              "falling back to raw-calldata-cost estimate (no contract execution)")

    print(
        f"\nworkload.num_vmnos = {args.num_vmnos} "
        f"(total VMNO settlements submitted per sweep point; "
        f"nproofs below is per-VMNO batch size, not workload size)"
    )

    anchor_threshold = None
    if anchor_address and args.mode in ("aggregate", "both"):
        print(f"\nprovisioning {args.num_attesters} attesters on the anchor...")
        attesters = pick_attesters(accounts, args.verifier_key_index, args.num_attesters)
        anchor_threshold = ensure_attesters(
            web3s[0], chain_id, anchor_address, accounts, attesters, args.threshold
        )
        # admin txs consumed nonces: re-read them before the sweep
        nonces = load_nonces(accounts, assignment, web3s)

    sweep_rows = []
    detail_dir = os.path.join(args.out_dir, "detail")
    os.makedirs(detail_dir, exist_ok=True)

    for nproofs in sweep:
        for run in range(1, args.runs + 1):

            offchain = load_offchain_log(args.offchain_logs_dir, nproofs, run)
            proof_ms_max = offchain.get("proof_ms_max", offchain.get("proof_ms"))

            print()
            print("=" * 60)
            print(f"nproofs/vmno={nproofs}  num_vmnos={args.num_vmnos}  run={run}  "
                  f"(off-chain source: {offchain['source']})")
            print("=" * 60)

            # Off-chain work summed over ALL VMNOs (each VMNO repeats the
            # same per-VMNO off-chain work): proof generation, and
            # aggregation + aggregation-verify (+ serialization if logged).
            proof_ms_total = offchain.get("proof_ms_total")
            agg_serialize_ms = offchain.get("agg_serialize_ms")
            agg_work_per_vmno_ms = (
                offchain["agg_ms"]
                + offchain["agg_verify_ms"]
                + (agg_serialize_ms or 0)
            )

            row = {
                "nproofs_per_vmno": nproofs,
                "num_vmnos": args.num_vmnos,
                "run": run,
                "offchain_source": offchain["source"],
                "offchain_proof_ms_max": proof_ms_max,
                "offchain_proof_ms_avg": offchain.get("proof_ms_avg"),
                "offchain_proof_ms_total": proof_ms_total,
                "offchain_param_ms": offchain.get("param_ms"),
                "offchain_single_verify_ms": offchain.get("single_verify_ms"),
                "offchain_agg_ms": offchain["agg_ms"],
                "offchain_agg_serialize_ms": agg_serialize_ms,
                "offchain_agg_verify_ms": offchain["agg_verify_ms"],
                "offchain_peak_mb": offchain["peak_mb"],
                "offchain_total_experiment_ms": offchain.get("total_experiment_ms"),
                "offchain_proof_work_ms_all_vmnos": (
                    proof_ms_total * args.num_vmnos if proof_ms_total is not None else None
                ),
                "offchain_aggregate_work_ms_all_vmnos": (
                    agg_work_per_vmno_ms * args.num_vmnos
                ),
            }

           
            # -------- individual: num_vmnos VMNOs x nproofs proofs ----------
            if args.mode in ("individual", "both"):

                # -------- commit: one commit(sid, h_cdr) tx per SESSION ----------
                # Used by the individual variant, so it is run once per sweep point.
                commit_gas = None
                commit_total_tx = nproofs * args.num_vmnos
                if args.include_commit:
                        run_salt = secrets.token_hex(4)  # fresh sids on every harness run
                        if commit_address:
                            calldata = []
                            for i in range(commit_total_tx):
                                        sid = Web3.keccak(
                                            text=f"zkroam-sid-{run_salt}-n{nproofs}-r{run}-{i}")
                                        h_cdr = Web3.keccak(make_calldata(32))
                                        calldata.append(Web3.to_bytes(
                                            hexstr=build_commit_calldata(web3s[0], sid, h_cdr)))
                            probe_hex = build_commit_calldata(
                                        web3s[0],
                                        Web3.keccak(text=f"zkroam-sid-probe-{run_salt}"),
                                        Web3.keccak(b"probe"),
                                    )
                            try:
                                        commit_gas = int(web3s[0].eth.estimate_gas({
                                            "from": accounts[0]["address"],
                                            "to": commit_address, "data": probe_hex,
                                        }) * 1.15)
                            except Exception as e:
                                        print(f"  estimate_gas failed ({e}), using 80000 fallback")
                                        commit_gas = 80_000
                            commit_to = commit_address
                            commit_bytes = len(calldata[0])
                        else:
                            # selector + 2 x bytes32, random stand-ins
                            one = make_calldata(4 + 64)
                            calldata = one
                            commit_gas = calldata_gas_cost(one) + COMMIT_EXEC_COST
                            commit_to = None
                            commit_bytes = len(one)
                
                        ctxs, cstats, _ = run_onchain_leg(
                                    label=f"commit_n{nproofs}_vmnos{args.num_vmnos}_r{run}",
                                    accounts=accounts, assignment=assignment, nonces=nonces,
                                    web3s=web3s, endpoints=endpoints, chain_id=chain_id,
                                    num_tx=commit_total_tx, calldata_bytes=calldata,
                                    gas_per_tx=commit_gas,
                                    rate=args.rate, workers=args.workers,
                                    receipt_workers=args.receipt_workers,
                                    receipt_timeout=args.receipt_timeout,
                                    poll_interval=args.poll_interval,
                                    receipt_timeout_per_tx=args.receipt_timeout_per_tx,
                                    resource_monitor=resource_monitor,
                                    to_address=commit_to,
                                )
                
                        for t in ctxs:
                                    nonces[t["account_index"]] = max(
                                        nonces[t["account_index"]], t["nonce"] + 1
                                    )
                
                        cg_vals = fetch_gas_used(ctxs, web3s) if commit_address else []
                        commit_gas_used = statistics.median(cg_vals) if cg_vals else commit_gas
                        clat = apply_block_time_latency(ctxs, web3s, args.latency_method)
                
                        row.update({
                                    "commit_total_tx": commit_total_tx,
                                    "commit_bytes_per_tx": commit_bytes,
                                    "commit_gas_limit_per_tx": commit_gas,
                                    "commit_gas_used_per_tx": commit_gas_used,
                                    "commit_total_gas_used": commit_gas_used * commit_total_tx,
                                    "commit_contract_called": bool(commit_address),
                                    "commit_confirmed": cstats["confirmed"],
                                    "commit_success_rate_pct": cstats["success_rate_pct"],
                                    "commit_submit_throughput_tx_s": cstats["submission_throughput_tx_s"],
                                    "commit_confirm_throughput_tx_s": cstats["confirmation_throughput_tx_s"],
                                    "commit_latency_p50_s": clat["p50"],
                                    "commit_latency_p99_s": clat["p99"],
                                    "commit_confirm_duration_s": cstats["confirm_duration_s"],
                                })
                
                        write_leg_detail(detail_dir, "commit", nproofs, args.num_vmnos, args.rate, args.workers, args.receipt_workers, run, ctxs, endpoints)
                
                        nonces = settle_network(
                                    "commit_n{}_r{}".format(nproofs, run),
                                    accounts, assignment, web3s, args.drain_timeout_s,
                                    resource_monitor,
                                )
                
                           
                #------- individual: num_vmnos VMNOs x nproofs proofs ----------
                individual_total_tx = nproofs * args.num_vmnos

                if cdr_verifier_address:
                    pA, pB, pC, pubsig = real_proof or (None, None, None, None)
                    calldata_hex = build_verify_proof_calldata(
                        web3s[0], pA, pB, pC, pubsig
                    )
                    calldata = Web3.to_bytes(hexstr=calldata_hex)
                    try:
                        gas = web3s[0].eth.estimate_gas({
                            "to": cdr_verifier_address, "data": calldata_hex,
                        })
                        gas = int(gas * 1.15)
                    except Exception as e:
                        print(f"  estimate_gas failed ({e}), using 400000 fallback")
                        gas = 400_000
                    leg_to = cdr_verifier_address
                else:
                    calldata = make_calldata(GROTH16_PROOF_BYTES)
                    gas = calldata_gas_cost(calldata) + EXEC_COST
                    leg_to = None

                txs, stats, rpc_stats = run_onchain_leg(
                    label=f"individual_n{nproofs}_vmnos{args.num_vmnos}_r{run}",
                    accounts=accounts, assignment=assignment, nonces=nonces,
                    web3s=web3s, endpoints=endpoints, chain_id=chain_id,
                    num_tx=individual_total_tx, calldata_bytes=calldata, gas_per_tx=gas,
                    rate=args.rate, workers=args.workers,
                    receipt_workers=args.receipt_workers,
                    receipt_timeout=args.receipt_timeout,
                    poll_interval=args.poll_interval,
                    receipt_timeout_per_tx=args.receipt_timeout_per_tx,
                    resource_monitor=resource_monitor,
                    to_address=leg_to,
                )

                for t in txs:
                    nonces[t["account_index"]] = max(
                        nonces[t["account_index"]], t["nonce"] + 1
                    )

                gas_used_vals = fetch_gas_used(txs, web3s) if cdr_verifier_address else []
                real_gas_median = statistics.median(gas_used_vals) if gas_used_vals else gas
                lat = apply_block_time_latency(txs, web3s, args.latency_method)

                row.update({
                    "individual_total_tx": individual_total_tx,
                    "individual_bytes_per_tx": len(calldata),
                    "individual_gas_limit_per_tx": gas,
                    "individual_gas_used_per_tx": real_gas_median,
                    "individual_total_gas_used": real_gas_median * individual_total_tx,
                    "individual_contract_called": bool(cdr_verifier_address),
                    "individual_confirmed": stats["confirmed"],
                    "individual_success_rate_pct": stats["success_rate_pct"],
                    "individual_submit_throughput_tx_s": stats["submission_throughput_tx_s"],
                    "individual_confirm_throughput_tx_s": stats["confirmation_throughput_tx_s"],
                    "individual_latency_p50_s": lat["p50"],
                    "individual_latency_p99_s": lat["p99"],
                    "individual_confirm_duration_s": stats["confirm_duration_s"],
                })

                if proof_ms_max is not None and stats["confirm_duration_s"] is not None:
                    row["individual_pipeline_latency_s"] = (
                        proof_ms_max / 1000.0
                        + (stats["confirm_duration_s"] or 0)
                    )

                write_leg_detail(detail_dir, "individual", nproofs, args.num_vmnos, args.rate, args.workers, args.receipt_workers, run, txs, endpoints)

                nonces = settle_network(
                    "individual_n{}_r{}".format(nproofs, run),
                    accounts, assignment, web3s, args.drain_timeout_s,
                    resource_monitor,
                )

            # -------- aggregate: num_vmnos VMNOs x 1 aggregate attestation ----
            if args.mode in ("aggregate", "both"):

                aggregate_total_tx = args.num_vmnos

                proof_bytes = estimate_aggregate_proof_bytes(
                    nproofs, offchain.get("aggregate_proof_bytes_measured")
                )

                if anchor_address:
                    # One DISTINCT settlementId per VMNO, relayed via
                    # relayAggregateProof, signed off-chain by num_attesters
                    # verifier identities and submitted through the FULL
                    # account pool.
                    attesters = pick_attesters(
                        accounts, args.verifier_key_index, args.num_attesters
                    )
                    domain_sep = anchor_domain_separator(chain_id, anchor_address)

                    calldata_list = []
                    for vmno_i in range(aggregate_total_tx):
                        # Stand-in payload; swap in the real serialized
                        # aggregate proof for a real commitment.
                        fake_proof_bytes = make_calldata(proof_bytes)
                        commitment = Web3.keccak(fake_proof_bytes)
                        settlement_id = Web3.keccak(
                            text=f"zkroam-settlement-n{nproofs}-run{run}-vmno{vmno_i}"
                        )
                        digest = attestation_digest(
                            domain_sep, settlement_id, commitment, nproofs
                        )
                        calldata_hex = build_relay_calldata_multi(
                            web3s[0], settlement_id, commitment, nproofs,
                            [a["address"] for a in attesters],
                            [sign_attestation(digest, a["private_key"])
                             for a in attesters],
                        )
                        calldata_list.append(Web3.to_bytes(hexstr=calldata_hex))
                    calldata = calldata_list

                    probe_id = Web3.keccak(text=f"zkroam-gas-probe-n{nproofs}-run{run}")
                    probe_commitment = Web3.keccak(b"probe")
                    probe_digest = attestation_digest(
                        domain_sep, probe_id, probe_commitment, nproofs
                    )
                    probe_calldata_hex = build_relay_calldata_multi(
                        web3s[0], probe_id, probe_commitment, nproofs,
                        [a["address"] for a in attesters],
                        [sign_attestation(probe_digest, a["private_key"])
                         for a in attesters],
                    )
                    try:
                        gas = web3s[0].eth.estimate_gas({
                            "from": accounts[0]["address"],
                            "to": anchor_address, "data": probe_calldata_hex,
                        })
                        gas = int(gas * 1.15)
                    except Exception as e:
                        print(f"  estimate_gas failed ({e}), using 200000 fallback")
                        gas = 200_000
                    leg_to = anchor_address
                    leg_accounts = None
                else:
                    calldata = make_calldata(proof_bytes)
                    gas = calldata_gas_cost(calldata) + EXEC_COST
                    leg_to = None
                    leg_accounts = None

                txs, stats, rpc_stats = run_onchain_leg(
                    label=f"aggregate_n{nproofs}_vmnos{args.num_vmnos}_r{run}",
                    accounts=accounts, assignment=assignment, nonces=nonces,
                    web3s=web3s, endpoints=endpoints, chain_id=chain_id,
                    num_tx=aggregate_total_tx, calldata_bytes=calldata, gas_per_tx=gas,
                    rate=args.rate, workers=args.workers,
                    receipt_workers=args.receipt_workers,
                    receipt_timeout=args.receipt_timeout,
                    poll_interval=args.poll_interval,
                    receipt_timeout_per_tx=args.receipt_timeout_per_tx,
                    resource_monitor=resource_monitor,
                    to_address=leg_to,
                    accounts_override=leg_accounts,
                )

                for t in txs:
                    nonces[t["account_index"]] = max(
                        nonces[t["account_index"]], t["nonce"] + 1
                    )

                gas_used_vals = fetch_gas_used(txs, web3s) if anchor_address else []
                real_gas = statistics.median(gas_used_vals) if gas_used_vals else gas
                lat = apply_block_time_latency(txs, web3s, args.latency_method)
                if anchor_address and len(gas_used_vals) < aggregate_total_tx:
                    print(
                        f"  warning: only {len(gas_used_vals)}/{aggregate_total_tx} "
                        f"aggregate anchor calls confirmed with gasUsed - check "
                        f"stats['reverted']/['timeout'] below for why."
                    )

                # Latency of the very first submitted tx (txs[0]), same
                # measure as the percentiles.
                first_tx_latency = txs[0]["latency_s"] if txs else None

                row.update({
                    "aggregate_total_tx": aggregate_total_tx,
                    "aggregate_num_attesters": args.num_attesters if anchor_address else None,
                    "anchor_threshold": anchor_threshold,
                    "aggregate_proof_bytes": proof_bytes,
                    "aggregate_proof_bytes_is_measured": bool(
                        offchain.get("aggregate_proof_bytes_measured")
                    ),
                    "aggregate_gas_limit_per_tx": gas,
                    "aggregate_gas_used_per_tx": real_gas,
                    "aggregate_total_gas_used": real_gas * aggregate_total_tx,
                    "aggregate_contract_called": bool(anchor_address),
                    "aggregate_confirmed": stats["confirmed"],
                    "aggregate_success_rate_pct": stats["success_rate_pct"],
                    "aggregate_submit_throughput_tx_s": stats["submission_throughput_tx_s"],
                    "aggregate_confirm_throughput_tx_s": stats["confirmation_throughput_tx_s"],
                    "aggregate_latency_p50_s": lat["p50"],
                    "aggregate_latency_p99_s": lat["p99"],
                    # Representative per-tx confirmation latency = p50.
                    "aggregate_latency_s": lat["p50"],
                    "aggregate_first_tx_latency_s": first_tx_latency,
                    "aggregate_confirm_duration_s": stats["confirm_duration_s"],
                })

                # off-chain: proof gen (slowest) + aggregation + agg verify
                # (+ serialization if logged), then on-chain confirm duration.
                if proof_ms_max is not None and stats["confirm_duration_s"] is not None:
                    row["aggregate_pipeline_latency_s"] = (
                        proof_ms_max / 1000.0
                        + offchain["agg_ms"] / 1000.0
                        + (agg_serialize_ms or 0) / 1000.0
                        + offchain["agg_verify_ms"] / 1000.0
                        + (stats["confirm_duration_s"] or 0)
                    )

                write_leg_detail(detail_dir, "aggregate", nproofs, args.num_vmnos, args.rate, args.workers, args.receipt_workers, run, txs, endpoints)

                nonces = settle_network(
                    "aggregate_n{}_r{}".format(nproofs, run),
                    accounts, assignment, web3s, args.drain_timeout_s,
                    resource_monitor,
                )

            # -------- head-to-head for this (nproofs, num_vmnos, run) --------
            if args.mode == "both":
                ind = row.get("individual_total_gas_used")
                agg = row.get("aggregate_total_gas_used")
                if ind and agg:
                    row["gas_savings_pct"] = (1 - (agg / ind)) * 100
                ind_lat = row.get("individual_pipeline_latency_s")
                agg_lat = row.get("aggregate_pipeline_latency_s")
                if ind_lat and agg_lat:
                    row["pipeline_speedup_x"] = ind_lat / agg_lat

            # -------- end-to-end per-session gas (commit + settlement) --------
            # session = one CDR. ZK-individual: commit + one verifyProof tx.
            # ZK-aggregate: commit + 1/nproofs of the per-VMNO anchor tx.
            cgu = row.get("commit_gas_used_per_tx")
            if cgu is not None:
                n_sessions = nproofs * args.num_vmnos
                ind_gas_tx = row.get("individual_gas_used_per_tx")
                agg_gas_tx = row.get("aggregate_gas_used_per_tx")
                if ind_gas_tx is not None:
                    row["e2e_individual_gas_per_session"] = cgu + ind_gas_tx
                    row["e2e_individual_total_gas"] = (cgu + ind_gas_tx) * n_sessions
                if agg_gas_tx is not None:
                    per_session = cgu + agg_gas_tx / nproofs
                    row["e2e_aggregate_gas_per_session"] = per_session
                    row["e2e_aggregate_total_gas"] = (
                        cgu * n_sessions + agg_gas_tx * args.num_vmnos
                    )
                    row["commit_share_of_aggregate_e2e_pct"] = 100.0 * cgu / per_session
                if (row.get("e2e_individual_total_gas")
                        and row.get("e2e_aggregate_total_gas")):
                    row["e2e_gas_savings_pct"] = (
                        1 - row["e2e_aggregate_total_gas"]
                        / row["e2e_individual_total_gas"]
                    ) * 100

            sweep_rows.append(row)

    if resource_monitor is not None:
        resource_monitor.stop()
        resource_monitor.write_csv(os.path.join(args.out_dir, f"{args.experiment_name}_{args.num_vmnos}_{args.rate}_{args.workers}_{args.receipt_workers}_resource_usage.csv"))

    write_sweep_summary(os.path.join(args.out_dir, f"{args.experiment_name}_{args.num_vmnos}_{args.rate}_{args.workers}_{args.receipt_workers}_sweep_summary.csv"), sweep_rows)
    print_sweep_highlights(sweep_rows)


# ============================================================
# Output writers
# ============================================================

def write_leg_detail(detail_dir, label, nproofs, num_vmnos, rate, workers, receipt_workers, run, transactions, endpoints):

    path = os.path.join(detail_dir, f"{label}_n{nproofs}_{num_vmnos}_{rate}_{workers}_{receipt_workers}_r{run}.csv")

    fields = ["tx_index", "sender", "rpc_node", "tx_hash", "nonce",
              "submit_ts", "confirm_ts", "latency_s", "poll_latency_s",
              "block_number", "block_ts", "gas_used", "status", "error"]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in transactions:
            poll_latency = item.get("poll_latency_s")
            if poll_latency is None and item["submit_ts"] and item["confirm_ts"]:
                poll_latency = item["confirm_ts"] - item["submit_ts"]
            latency = item.get("latency_s")
            if latency is None:
                latency = poll_latency
            writer.writerow({
                "tx_index": item["tx_index"],
                "sender": item["sender"],
                "rpc_node": endpoints[item["rpc_index"]]["name"],
                "tx_hash": item["tx_hash"],
                "nonce": item["nonce"],
                "submit_ts": item["submit_ts"],
                "confirm_ts": item["confirm_ts"],
                "latency_s": latency,
                "poll_latency_s": poll_latency,
                "block_number": item["block_number"],
                "block_ts": item.get("block_ts"),
                "gas_used": item.get("gas_used"),
                "status": item["status"],
                "error": item["error"],
            })


def write_sweep_summary(path, rows):
    """Write rows using the fixed SWEEP_SUMMARY_FIELDS schema so the CSV
    always has the same columns/order; missing values are left empty."""
    if not rows:
        return

    # Keep any unexpected extra keys at the end rather than dropping them.
    extras = []
    for r in rows:
        for k in r.keys():
            if k not in SWEEP_SUMMARY_FIELDS and k not in extras:
                extras.append(k)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=SWEEP_SUMMARY_FIELDS + extras, restval=""
        )
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in r.items()})

    print(f"\nWrote: {path}")


def print_sweep_highlights(rows):
    if not rows:
        return

    by_n = defaultdict(list)
    for r in rows:
        by_n[r["nproofs_per_vmno"]].append(r)

    print()
    print("=" * 76)
    print("SWEEP HIGHLIGHTS (median across runs per nproofs/VMNO level)")
    print("=" * 76)
    print(f"{'nproofs':>8} {'num_vmnos':>10} {'ind_gas_total':>14} {'agg_gas_total':>14} "
          f"{'gas_save_%':>10} {'pipeline_x':>11} {'e2e_ind/sess':>13} "
          f"{'e2e_agg/sess':>13} {'e2e_save_%':>11}")

    for n in sorted(by_n):
        rs = by_n[n]
        vmnos = rs[0]["num_vmnos"]
        ind_gas = [r["individual_total_gas_used"] for r in rs if "individual_total_gas_used" in r]
        agg_gas = [r["aggregate_total_gas_used"] for r in rs if "aggregate_total_gas_used" in r]
        save = [r["gas_savings_pct"] for r in rs if "gas_savings_pct" in r]
        speed = [r["pipeline_speedup_x"] for r in rs if "pipeline_speedup_x" in r]
        e_ind = [r["e2e_individual_gas_per_session"] for r in rs if "e2e_individual_gas_per_session" in r]
        e_agg = [r["e2e_aggregate_gas_per_session"] for r in rs if "e2e_aggregate_gas_per_session" in r]
        e_sav = [r["e2e_gas_savings_pct"] for r in rs if "e2e_gas_savings_pct" in r]

        print(
            f"{n:>8} {vmnos:>10} "
            f"{statistics.median(ind_gas) if ind_gas else 0:>14,.0f} "
            f"{statistics.median(agg_gas) if agg_gas else 0:>14,.0f} "
            f"{statistics.median(save) if save else 0:>9.1f}% "
            f"{statistics.median(speed) if speed else 0:>10.2f}x "
            f"{statistics.median(e_ind) if e_ind else 0:>13,.0f} "
            f"{statistics.median(e_agg) if e_agg else 0:>13,.0f} "
            f"{statistics.median(e_sav) if e_sav else 0:>10.1f}%"
        )

    print("=" * 76)


if __name__ == "__main__":
    main()
