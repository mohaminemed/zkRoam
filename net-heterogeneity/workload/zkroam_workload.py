#!/usr/bin/env python3
"""
zkroam_workload.py

Custom transaction WORKLOAD for the Besu QBFT benchmark harness
(benchmark.py, the script you pasted), modeling the on-chain leg of your
zkRoam CDR circuit + SnarkPack aggregation pipeline
(snarkpack_aggregation/).

WHAT THIS MODELS
------------------------------------------------------------------
Your Rust binary already measures the OFF-CHAIN cost of a roaming
settlement: generating `nproofs` Groth16 CDR proofs, then aggregating
them with SnarkPack (output/experiment_log_<n>_<run>.json).

This script models what happens next: getting a settlement result
on-chain. It benchmarks two competing strategies, at the same
nproofs sweep you already used (8/64/128/256/512/1024/2048, 10 runs):

  * "individual" - post each of the `nproofs` Groth16 CDR proofs as
    its own transaction (no aggregation, one verify-tx per operator
    settlement record).
  * "aggregate"  - do the SnarkPack aggregation off-chain (already
    measured by your Rust binary) and post ONE transaction carrying
    the aggregate proof.

For each nproofs level it submits real transactions through
benchmark.py's producer/poller, gets submission throughput +
confirmation latency + gas cost, then stitches that together with
your measured off-chain numbers (proof-gen time, aggregation time,
aggregation-verify time) into one end-to-end pipeline comparison:

    individual pipeline = max(proof_gen_i) + max(individual tx confirm_i)
    aggregate  pipeline = max(proof_gen_i) + aggregation_time
                           + aggregate tx confirm

This is the number that actually demonstrates the value of
aggregation: fewer/smaller on-chain footprint vs proof count.

WORKLOAD SIZE: nproofs (per-VMNO) vs num_vmnos (total workload)
------------------------------------------------------------------
`nproofs` (the sweep value, e.g. 8/64/128/...) is the number of CDR
proofs a SINGLE VMNO settlement bundles together - it's what your
Rust binary swept and what `--offchain-logs-dir` has logs for.

`num_vmnos` (set in the YAML config, see below) is how many such
VMNO settlements are actually submitted on-chain in this run - i.e.
the size of the REAL total workload, roughly "how many operator
accounts are settling right now". It is unrelated to nproofs and
does not come from the Rust sweep at all.

The two combine like this per (nproofs, run) sweep point:

    individual leg -> nproofs * num_vmnos total transactions
                       (each VMNO posts nproofs individual proof-verify txs)
    aggregate  leg -> num_vmnos total transactions
                       (each VMNO posts exactly 1 aggregate-anchor tx)

So `num_vmnos` is the dial for "how big is the real workload", while
`nproofs` is the dial for "how big is one VMNO's settlement batch".
Do not conflate the two - a previous version of this script used a
single `--num-tx` flag for this and multiplied it into the individual
leg only, which made it easy to lose track of which knob you were
turning. Both are now explicit, separate config fields.

CONFIG FILE
------------------------------------------------------------------
All configuration now lives in a YAML file (default: config.yml,
override with --config path/to/file.yml). See the bundled
config.example.yml for the full schema and inline comments. There
are no other CLI flags - edit the YAML and re-run.

IMPORTANT ASSUMPTIONS (read before trusting the gas numbers)
------------------------------------------------------------------
1. There is no real EVM execution cost to measure - only the CALLDATA cost
   of getting proof bytes onto the chain (EIP-2028: 16 gas/nonzero byte,
   4 gas/zero byte, + 21000 base) - UNLESS contracts.deployed_contracts
   points at a real deploy_verifiers.py output, in which case real
   verifyProof()/anchorAggregateProof() calls are made and real gasUsed
   is fetched from receipts.
2. SnarkPack aggregate-proof size is estimated analytically (BLS12-381
   compressed point sizes x an O(log nproofs) TIPP/GIPA transcript).
   This is a stated approximation, not a serialized-byte-count.
   For real numbers: add one line to your Rust main.rs -
       let mut buf = vec![];
       agg_proof.serialize_compressed(&mut buf).unwrap();
   and store buf.len() as "aggregate_proof_bytes" in ExperimentLog.
   This script will use that field automatically if present.
3. "individual" mode posts a full compressed Groth16 proof (A,B,C =
   G1+G2+G1 = 48+96+48 = 192 bytes) per transaction, not just a hash
   commitment - the pessimistic case for the no-aggregation baseline.
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
from collections import defaultdict
from types import SimpleNamespace

import yaml

from besu_benchmark import (  # your pasted script, save it as besu_benchmark.py
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
# Deployed-contract wiring (see deploy_verifiers.py)
# ============================================================

# Minimal ABI fragments - enough to encode calls, no need for the full
# compiler output unless you also want to decode return values/events.
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


# ============================================================
# EIP-712 attestation signing (matches SnarkPackAggregateAnchor.sol's
# DOMAIN_SEPARATOR / ATTEST_TYPEHASH / _attestationDigest exactly - see
# the round-trip test in chat before this was wired in here).
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
    extra 'Ethereum Signed Message' prefix - the contract's ecrecover
    call expects the bare "\\x19\\x01"-prefixed digest we just built).
    eth_keys naturally emits low-s signatures, so this also satisfies
    the contract's malleability check without any extra normalization.
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

# Known-valid BN254 G1/G2 points, lifted straight from the verifier's own
# vk constants (IC0/IC1 are valid G1 points, the gamma coordinates a
# valid G2 point). Used as the default "dummy" proof so the on-chain
# precompile calls (ecAdd/ecMul/ecPairing) run to completion and the
# gas you measure reflects a REAL full verification cost - the proof
# will be cryptographically invalid (verifyProof returns false, but the
# transaction still succeeds; this contract never reverts on a bad
# proof), so this is honest for gas/latency benchmarking, not for
# correctness testing. Pass proof_json/public_json (snarkjs
# proof.json / public.json) for a real proof instead.
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
    # snarkjs G2 points are stored [x, y] with each coordinate as [c0, c1];
    # Solidity's uint[2][2] pB expects the coordinates swapped (c1, c0)
    # relative to snarkjs's own JSON order - this is the standard
    # snarkjs-exporter convention baked into the generated verifier above.
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


def build_anchor_calldata(w3, settlement_id: bytes, commitment: bytes, nproofs: int):
    """Direct anchorAggregateProof(onlyVerifier) calldata - kept for
    completeness/smoke-testing; the sweep driver's aggregate leg now uses
    build_relay_calldata()/relayAggregateProof instead, since that path
    doesn't require msg.sender itself to be an authorized verifier."""
    contract = w3.eth.contract(abi=AGGREGATE_ANCHOR_ABI)
    return contract.encode_abi(
        abi_element_identifier="anchorAggregateProof",
        args=[settlement_id, commitment, nproofs],
    )


def fetch_gas_used(transactions, web3s):
    """Post-hoc fetch of real gasUsed for confirmed txs (besu_benchmark.py's
    poll_receipt doesn't capture it, only status/block_number)."""
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


# ============================================================
# BLS12-381 / SnarkPack size constants
# ============================================================

G1_COMPRESSED_BYTES = 48
G2_COMPRESSED_BYTES = 96
GT_BYTES = 576          # Fq12, uncompressed (ark-serialize has no GT compression)
FR_BYTES = 32

GROTH16_PROOF_BYTES = (2 * G1_COMPRESSED_BYTES) + G2_COMPRESSED_BYTES  # A,B,C = 192B

# EIP-2028 calldata gas
GAS_PER_ZERO_BYTE = 4
GAS_PER_NONZERO_BYTE = 16
BASE_TX_GAS = 21_000
EXEC_COST = 206000  


# ============================================================
# Reference off-chain numbers (medians from YOUR
# output/experiment_log_<n>_<run>.json, 10 runs each), used only
# as a fallback when offchain.logs_dir doesn't have a matching
# file for a given (nproofs, run).
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
# Off-chain log loading (your Rust output/ directory)
# ============================================================

def load_offchain_log(offchain_dir, nproofs, run):
    """
    Look for output/experiment_log_<nproofs>_<run>.json produced by your
    Rust binary. Falls back to REFERENCE_OFFCHAIN_STATS if missing.
    """

    record = {
        "source": "reference_fallback",
        "aggregate_proof_bytes_measured": None,
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
                    "agg_ms": d["aggregation_time_ms"],
                    "agg_verify_ms": d["aggregation_verify_time_ms"],
                    "peak_mb": d["peak_memory_bytes"] / (1024 * 1024),
                    "aggregate_proof_bytes_measured": d.get("aggregate_proof_bytes"),
                }
            )
            return record

    ref = REFERENCE_OFFCHAIN_STATS.get(nproofs)
    if ref is None:
        # nearest neighbor fallback for sweep values outside the reference table
        nearest = min(REFERENCE_OFFCHAIN_STATS, key=lambda k: abs(k - nproofs))
        ref = REFERENCE_OFFCHAIN_STATS[nearest]

    record.update(
        {
            "proof_ms_max": ref["proof_ms"],
            "proof_ms_avg": ref["proof_ms"],
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

    Modeled structure (see module docstring for caveats):
      - com_ab, com_c, ip_ab: 3 GT commitments
      - agg_c: 1 G1 element
      - TIPP/GIPA transcript: ceil(log2(nproofs)) rounds, each with
        ~4 GT + 4 G1 elements
      - final_A (G1), final_B (G2), final_C (G1), final_r (Fr)
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
    """
    Random bytes stand in for serialized curve points/field elements,
    which are effectively random from a calldata-gas-cost point of view
    (almost all bytes nonzero) - a more realistic stand-in than zero-
    filled placeholder data.
    """
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
    carries calldata as `data` and uses an explicit gas limit sized
    for that calldata (instead of a flat 21000-gas value transfer).

    `calldata` is either:
      - a single `bytes` object, reused verbatim for all `num_tx`
        transactions (fine for the "individual" leg: verifyProof() is a
        pure/stateless view function, so calling it repeatedly with the
        same input is harmless and models "replay the same proof" on
        purpose - see the module docstring).
      - a list/tuple of `num_tx` distinct `bytes` objects, one per
        transaction. REQUIRED for the "aggregate" leg: each VMNO's
        anchorAggregateProof() call must carry its own settlementId, or
        every call after the first reverts with "already attested"
        against the SAME (settlementId, sender) pair - see the chat
        note on why gas/tx was pinned at the 150000 fallback.

    `num_tx` here is the TOTAL transaction count for this on-chain leg
    (already resolved by the caller from nproofs * num_vmnos for the
    individual leg, or num_vmnos for the aggregate leg) - it is not a
    config field in its own right, see WORKLOAD SIZE in the module
    docstring.
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
):
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
        # per-VMNO proof-count levels, matches run_experiments.sh's PROOFS array
        "nproofs": list(DEFAULT_SWEEP),
        "runs": 1,
        "mode": "both",  # individual | aggregate | both
    },
    "workload": {
        # Total number of VMNO settlements submitted on-chain per sweep
        # point - the REAL total workload size. NOT the nproofs sweep
        # value. See "WORKLOAD SIZE" in the module docstring.
        # individual leg total tx = nproofs * num_vmnos
        # aggregate  leg total tx = num_vmnos
        "num_vmnos": 100,
    },
    "offchain": {
        "logs_dir": "snarkpack_aggregation/output",
    },
    "contracts": {
        "verifier_address": None,
        "deployed_contracts": "deployed_contracts.json",
        "proof_json": None,
        "public_json": None,
        # Index into accounts.json for the identity that SIGNS aggregate
        # attestations (relayAggregateProof). Must be authorized on-chain
        # via deploy_verifiers.py (default pool includes accounts[0]).
        # Submission/relaying still uses the full account pool regardless
        # of this value - this only picks whose signature goes in the
        # calldata, not who pays gas.
        "verifier_key_index": 0,
    },
    "execution": {
        "rate": 500.0,
        "workers": 16,
        "receipt_workers": 16,
        "receipt_timeout": 120.0,
        "poll_interval": 0.25,
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
            f"--config /path/to/your.yml."
        )

    with open(path) as f:
        user_cfg = yaml.safe_load(f) or {}

    cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)

    # Flatten into a single namespace for convenience at call sites,
    # mirroring the old argparse attribute names where they still apply.
    args = SimpleNamespace(
        experiment_name=cfg["experiment"]["name"],
        topology=cfg["network"]["topology"],
        accounts=cfg["network"]["accounts"],
        sweep=cfg["sweep"]["nproofs"],
        runs=int(cfg["sweep"]["runs"]),
        mode=cfg["sweep"]["mode"],
        num_vmnos=int(cfg["workload"]["num_vmnos"]),
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
        monitor_resources=bool(cfg["monitoring"]["enabled"]),
        monitor_interval=float(cfg["monitoring"]["interval"]),
        out_dir=cfg["output"]["out_dir"],
    )

    if isinstance(args.sweep, str):
        args.sweep = [int(x) for x in args.sweep.split(",") if str(x).strip()]
    else:
        args.sweep = [int(x) for x in args.sweep]

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

    topology = load_json(args.topology)
    endpoints = build_rpc_endpoints(topology)
    web3s, chain_id = connect_web3(endpoints)

    accounts = load_accounts(args.accounts)
    if not accounts:
        raise RuntimeError("No workload accounts found.")

    assignment = assign_accounts(accounts, endpoints)
    nonces = load_nonces(accounts, assignment, web3s)

    deployed = load_deployed_contracts(args.deployed_contracts)

    cdr_verifier_address = None
    if args.verifier_address:
        cdr_verifier_address = Web3.to_checksum_address(args.verifier_address)
    elif deployed and deployed.get("cdr_verifier"):
        cdr_verifier_address = Web3.to_checksum_address(deployed["cdr_verifier"]["address"])

    anchor_address = None
    if deployed and deployed.get("aggregate_anchor"):
        anchor_address = Web3.to_checksum_address(deployed["aggregate_anchor"]["address"])

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
        print(f"aggregate leg  -> real anchorAggregateProof() calls on {anchor_address}")
    else:
        print("aggregate leg  -> no deployed aggregate_anchor found, "
              "falling back to raw-calldata-cost estimate (no contract execution)")

    print(
        f"\nworkload.num_vmnos = {args.num_vmnos} "
        f"(total VMNO settlements submitted per sweep point; "
        f"nproofs below is per-VMNO batch size, not workload size)"
    )

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

            row = {
                "nproofs_per_vmno": nproofs,
                "num_vmnos": args.num_vmnos,
                "run": run,
                "offchain_source": offchain["source"],
                "offchain_proof_ms_max": proof_ms_max,
                "offchain_agg_ms": offchain["agg_ms"],
                "offchain_agg_verify_ms": offchain["agg_verify_ms"],
                "offchain_peak_mb": offchain["peak_mb"],
            }

            # -------- individual: each of num_vmnos VMNOs posts nproofs --------
            # -------- full Groth16 proofs -> nproofs * num_vmnos total tx ------
            if args.mode in ("individual", "both"):

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
                        gas = int(gas * 1.15)  # margin, estimate_gas can undershoot for view fns
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
                    resource_monitor=resource_monitor,
                    to_address=leg_to,
                )

                # advance nonces past this leg so the next leg doesn't collide
                for t in txs:
                    nonces[t["account_index"]] = max(
                        nonces[t["account_index"]], t["nonce"] + 1
                    )

                gas_used_vals = fetch_gas_used(txs, web3s) if cdr_verifier_address else []
                real_gas_median = statistics.median(gas_used_vals) if gas_used_vals else gas

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
                    "individual_latency_p50_s": stats["latency_p50_s"],
                    "individual_latency_p99_s": stats["latency_p99_s"],
                    "individual_confirm_duration_s": stats["confirm_duration_s"],
                })

                if proof_ms_max is not None and stats["confirm_duration_s"] is not None:
                    row["individual_pipeline_latency_s"] = (
                        proof_ms_max / 1000.0
                        + (stats["confirm_duration_s"] or 0)
                    )

                write_leg_detail(detail_dir, "individual", nproofs, args.num_vmnos, run, txs, endpoints)

            # -------- aggregate: each of num_vmnos VMNOs posts 1 SnarkPack ----
            # -------- aggregate proof -> num_vmnos total tx --------------------
            if args.mode in ("aggregate", "both"):

                aggregate_total_tx = args.num_vmnos

                proof_bytes = estimate_aggregate_proof_bytes(
                    nproofs, offchain.get("aggregate_proof_bytes_measured")
                )

                if anchor_address:
                    # One DISTINCT settlementId per VMNO in this leg - the
                    # replay guard is keyed on (settlementId, verifier),
                    # so each VMNO's own settlement needs its own id
                    # regardless of which path anchors it.
                    #
                    # Relayed via relayAggregateProof, signed off-chain by
                    # ONE verifier identity (contracts.verifier_key_index,
                    # default accounts[0] - matches deploy_verifiers.py's
                    # constructor(initialVerifier)) and submitted through
                    # the FULL account pool, round-robined exactly like
                    # every other leg. No addVerifier() pool needed:
                    # verifier identity comes from the signature, not
                    # msg.sender, so submission load is decoupled from
                    # Besu's ~200 in-flight nonce-gap cap per sender -
                    # that's the whole point of the relay path over
                    # anchorAggregateProof (see the contract's design note
                    # and the chat discussion of this exact fix).
                    verifier_key_index = args.verifier_key_index
                    verifier_account = accounts[verifier_key_index]
                    verifier_priv_key = verifier_account["private_key"]
                    domain_sep = anchor_domain_separator(chain_id, anchor_address)

                    calldata_list = []
                    for vmno_i in range(aggregate_total_tx):
                        # Stand-in payload here since we don't have your
                        # real per-VMNO serialized aggregate proof bytes
                        # in this sandbox - swap in
                        # agg_proof.serialize_compressed() output on your
                        # side for a real commitment.
                        fake_proof_bytes = make_calldata(proof_bytes)
                        commitment = Web3.keccak(fake_proof_bytes)
                        settlement_id = Web3.keccak(
                            text=f"zkroam-settlement-n{nproofs}-run{run}-vmno{vmno_i}"
                        )
                        digest = attestation_digest(
                            domain_sep, settlement_id, commitment, nproofs
                        )
                        signature = sign_attestation(digest, verifier_priv_key)
                        calldata_hex = build_relay_calldata(
                            web3s[0], settlement_id, commitment, nproofs,
                            verifier_account["address"], signature,
                        )
                        calldata_list.append(Web3.to_bytes(hexstr=calldata_hex))
                    calldata = calldata_list

                    # Gas depends on batch size (1 signer here) and the
                    # first-time-attestation storage writes, not on the
                    # specific settlementId/signature values, so one
                    # estimate suffices - probe id never actually submitted.
                    probe_id = Web3.keccak(text=f"zkroam-gas-probe-n{nproofs}-run{run}")
                    probe_commitment = Web3.keccak(b"probe")
                    probe_digest = attestation_digest(
                        domain_sep, probe_id, probe_commitment, nproofs
                    )
                    probe_sig = sign_attestation(probe_digest, verifier_priv_key)
                    probe_calldata_hex = build_relay_calldata(
                        web3s[0], probe_id, probe_commitment, nproofs,
                        verifier_account["address"], probe_sig,
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
                    # No accounts_override: relaying is permissionless by
                    # design, so use the full account pool like every
                    # other leg - this is exactly what removes the
                    # nonce-gap ceiling instead of just working around it.
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
                    resource_monitor=resource_monitor,
                    to_address=leg_to,
                    accounts_override=leg_accounts,
                )

                for t in txs:
                    nonces[t["account_index"]] = max(
                        nonces[t["account_index"]], t["nonce"] + 1
                    )

                gas_used_vals = fetch_gas_used(txs, web3s) if anchor_address else []
                # Now that every VMNO has its own settlementId, all
                # aggregate_total_tx calls should succeed independently
                # (not just the first) - median across all of them, same
                # as the individual leg, instead of taking txs[0] alone.
                real_gas = statistics.median(gas_used_vals) if gas_used_vals else gas
                if anchor_address and len(gas_used_vals) < aggregate_total_tx:
                    print(
                        f"  warning: only {len(gas_used_vals)}/{aggregate_total_tx} "
                        f"aggregate anchor calls confirmed with gasUsed - check "
                        f"stats['reverted']/['timeout'] below for why."
                    )

                row.update({
                    "aggregate_total_tx": aggregate_total_tx,
                    "aggregate_proof_bytes": proof_bytes,
                    "aggregate_proof_bytes_is_measured": bool(
                        offchain.get("aggregate_proof_bytes_measured")
                    ),
                    "aggregate_gas_limit_per_tx": gas,
                    "aggregate_gas_used_per_tx": real_gas,
                    "aggregate_total_gas_used": real_gas * aggregate_total_tx,
                    "aggregate_contract_called": bool(anchor_address),
                    "aggregate_confirmed": stats["confirmed"],
                    "aggregate_latency_s": (
                        txs[0]["confirm_ts"] - txs[0]["submit_ts"]
                        if txs[0]["confirm_ts"] and txs[0]["submit_ts"]
                        else None
                    ),
                })

                agg_tx_latency = row.get("aggregate_latency_s") or 0
                if proof_ms_max is not None:
                    row["aggregate_pipeline_latency_s"] = (
                        proof_ms_max / 1000.0
                        + offchain["agg_ms"] / 1000.0
                        + agg_tx_latency
                    )

                write_leg_detail(detail_dir, "aggregate", nproofs, args.num_vmnos, run, txs, endpoints)

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

            sweep_rows.append(row)

    if resource_monitor is not None:
        resource_monitor.stop()
        resource_monitor.write_csv(os.path.join(args.out_dir, f"{args.experiment_name}_resource_usage.csv"))

    write_sweep_summary(os.path.join(args.out_dir, f"{args.experiment_name}_sweep_summary.csv"), sweep_rows)
    print_sweep_highlights(sweep_rows)


# ============================================================
# Output writers
# ============================================================

def write_leg_detail(detail_dir, label, nproofs, num_vmnos, run, transactions, endpoints):
    path = os.path.join(detail_dir, f"{label}_n{nproofs}_vmnos{num_vmnos}_r{run}.csv")

    fields = ["tx_index", "sender", "rpc_node", "tx_hash", "nonce",
              "submit_ts", "confirm_ts", "latency_s", "block_number",
              "gas_used", "status", "error"]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in transactions:
            latency = None
            if item["submit_ts"] and item["confirm_ts"]:
                latency = item["confirm_ts"] - item["submit_ts"]
            writer.writerow({
                "tx_index": item["tx_index"],
                "sender": item["sender"],
                "rpc_node": endpoints[item["rpc_index"]]["name"],
                "tx_hash": item["tx_hash"],
                "nonce": item["nonce"],
                "submit_ts": item["submit_ts"],
                "confirm_ts": item["confirm_ts"],
                "latency_s": latency,
                "block_number": item["block_number"],
                "gas_used": item.get("gas_used"),
                "status": item["status"],
                "error": item["error"],
            })


def write_sweep_summary(path, rows):
    if not rows:
        return

    fieldnames = []
    for r in rows:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

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
          f"{'gas_save_%':>10} {'pipeline_x':>11}")

    for n in sorted(by_n):
        rs = by_n[n]
        vmnos = rs[0]["num_vmnos"]
        ind_gas = [r["individual_total_gas_used"] for r in rs if "individual_total_gas_used" in r]
        agg_gas = [r["aggregate_total_gas_used"] for r in rs if "aggregate_total_gas_used" in r]
        save = [r["gas_savings_pct"] for r in rs if "gas_savings_pct" in r]
        speed = [r["pipeline_speedup_x"] for r in rs if "pipeline_speedup_x" in r]

        print(
            f"{n:>8} {vmnos:>10} "
            f"{statistics.median(ind_gas) if ind_gas else 0:>14,.0f} "
            f"{statistics.median(agg_gas) if agg_gas else 0:>14,.0f} "
            f"{statistics.median(save) if save else 0:>9.1f}% "
            f"{statistics.median(speed) if speed else 0:>10.2f}x"
        )

    print("=" * 76)


if __name__ == "__main__":
    main()