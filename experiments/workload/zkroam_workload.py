#!/usr/bin/env python3
"""
zkroam_workload.py

zkRoam on-chain workload, run against the REAL settlement contracts:

  AgreementIndividual  one Groth16 verifyProof per session (UE commits h_cdr first)
  AgreementAggregate   SnarkPack aggregate attested through SnarkPackAggregateAnchor,
                       then ONE settleBatch per VMNO. No per-session commit.

Same session model, escrow, rates and payouts as AgreementPlain (plain_workload.py),
so the three paths differ only in how the cost is validated.

  sweep.nproofs      -> sessions ONE VMNO settles together
  workload.num_vmnos -> number of VMNOs (groups of sessions)

Legs (setup - deploy, mint, approve, startRoamingSession - is not measured):

  individual  commit    N txs   UE -> submitCDR(sessionId, h_cdr)
              individual N txs  terminateRoamingSession(sessionId, proof, cost)
  aggregate   anchor    num_vmnos txs  relayAggregateProof (exactly t = threshold signatures,
                                         taken from a committee of num_attesters)
              settle    num_vmnos txs  settleBatch(ueSigs, batchId, aggProofHash, ids, costs, cdrHashes)
              (no submitCDR: each UE signs (agreement, chainid, sessionId, cdrHash) off-chain,
               settleBatch verifies that signature on-chain against Session.ueAddress)

Each leg runs on a FRESH agreement instance. Columns: individual_commit_*,
individual_*, aggregate_anchor_*, aggregate_settle_*, aggregate_* (combined),
e2e_* (individual = commit + settle, aggregate = anchor + settle, no commit),
gas_savings_pct (settlement only), e2e_gas_savings_pct, *_valid_all
(False when any tx of the path failed: do not compare it).

VERIFIER MODE: without contracts.proof_json/public_json the individual leg settles
through BenchGroth16Verifier (real pairing, returns true; ~3k gas wrapper overhead,
see column individual_verifier_mode). With a real snarkjs proof it calls the real
Groth16Verifier and the rates are taken from the proof's public inputs.

TRUST NOTE: the aggregate proof is verified OFF-CHAIN by the attesters; on-chain
only their threshold attestation is checked. The aggregate path has no on-chain UE
commit; instead cdrHash provenance is enforced on-chain by a per-session UE
signature (EIP-2098, 64 B/session) checked in settleBatch. A UE can still sign two
hashes for one session (no write-once commit), which gas numbers do not capture.

Off-chain proving/aggregation times come from snarkpack_aggregation/output
(experiment_log_<n>_<run>.json) or the reference fallback.
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
from types import SimpleNamespace

import yaml
from web3 import Web3
from eth_abi import encode as eth_abi_encode
from eth_keys import keys as eth_keys

from besu_benchmark import (
    ResourceMonitor, build_rpc_endpoints, connect_web3, load_accounts,
    assign_accounts, load_json,
)
from agreement_common import (
    Env, Token, run_leg, settle_leg, estimate_gas, check_gas_fits_block,
    leg_columns, deploy_agreement, deploy_contract, load_artifact, forge_artifact,
    make_ue_accounts, fund_ue_accounts, session_plan, field_cdr_hash,
    start_sessions, commit_leg, _send_and_wait,
)

# ============================================================
# Anchor ABI + EIP-712 attestation signing (unchanged)
# ============================================================

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

# Public-signal order of the CDR circuit (snarkjs public.json) and therefore of
# Groth16Verifier.verifyProof's uint[5]. AgreementIndividual.terminateRoamingSession
# must build its array in exactly this order.
PS_RSMS, PS_RDATA, PS_RVOICE, PS_COST, PS_HASH = range(5)


def pub_signals(rates, cost, cdr_hash_int):
    """Verifier public inputs in circuit order: [r_sms, r_data, r_voice, cost, cdrHash]."""
    return [int(rates["sms"]), int(rates["data"]), int(rates["voice"]),
            int(cost), int(cdr_hash_int)]


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


def make_calldata(n_bytes):
    """Random bytes stand in for serialized curve points/field elements."""
    return secrets.token_bytes(n_bytes)




# ============================================================
# Batch commitment (mirror of AgreementAggregate._commitment)
# ============================================================

BATCH_DOMAIN = Web3.keccak(text="zkRoam.AgreementAggregate.batch.v1")


def sessions_digest(sids, costs, cdrs):
    return Web3.keccak(b"".join(
        eth_abi_encode(["uint256", "uint256", "bytes32"], [s, c, h])
        for s, c, h in zip(sids, costs, cdrs)))


# UE signature over the CDR hash (mirror of AgreementAggregate.ueCDRDigest)
UE_CDR_DOMAIN = Web3.keccak(text="zkRoam.AgreementAggregate.ueCDR.v1")


def ue_cdr_digest(agreement, chain_id, session_id, cdr_hash: bytes) -> bytes:
    return Web3.keccak(eth_abi_encode(
        ["bytes32", "address", "uint256", "uint256", "bytes32"],
        [UE_CDR_DOMAIN, Web3.to_checksum_address(agreement), chain_id, session_id, cdr_hash]))


def sign_cdr_compact(digest: bytes, private_key_hex: str) -> bytes:
    """EIP-2098 compact signature r || (yParity<<255 | s). eth_keys gives v in {0,1}, low-s."""
    pk = eth_keys.PrivateKey(bytes.fromhex(private_key_hex.replace("0x", "")))
    sig = pk.sign_msg_hash(digest)
    return sig.r.to_bytes(32, "big") + (sig.s | (sig.v << 255)).to_bytes(32, "big")


def batch_commitment(agreement, chain_id, batch_id, agg_proof_hash, rates, sids, costs, cdrs):
    return Web3.keccak(eth_abi_encode(
        ["bytes32", "address", "uint256", "bytes32", "uint256", "bytes32",
         "uint256", "uint256", "uint256", "bytes32"],
        [BATCH_DOMAIN, agreement, chain_id, batch_id, len(sids), agg_proof_hash,
         rates["sms"], rates["data"], rates["voice"],
         sessions_digest(sids, costs, cdrs)]))


def settlement_id_of(agreement, batch_id):
    return Web3.keccak(eth_abi_encode(["address", "bytes32"], [agreement, batch_id]))


# ============================================================
# Config
# ============================================================

DEFAULT_CONFIG = {
    "experiment": {"name": "zkroam_benchmark"},
    "network": {
        "topology": "networkFiles/topology.json",
        "accounts": "accounts/accounts.json",
    },
    "sweep": {
        "nproofs": list(DEFAULT_SWEEP),
        "runs": 1,
        "mode": "both",          # individual | aggregate | both
    },
    "workload": {
        # individual: nproofs * num_vmnos sessions (commit + terminate txs each)
        # aggregate : num_vmnos batches (1 anchor tx + 1 settleBatch tx each)
        "num_vmnos": 8,
        # attester committee size m (authorized verifiers provisioned on the anchor)
        "num_attesters": 5,
        # anchor threshold t; each relay submits exactly t signatures (extra ones only
        # cost gas: the anchor stores every signature even after finality).
        # null = leave the on-chain value as is
        "threshold": 3,
        # agreement rates (ignored when a real proof is given: its public
        # inputs fix the rates)
        "rate_sms": 5,
        "rate_voice": 3,
        "rate_data": 2,
        "estimate_margin_pct": 20,
        "setup_chunk": 500,
        # native balance per UE; null = auto (gas_price * 150k * #commit txs)
        "ue_fund_wei": None,
    },
    "offchain": {"logs_dir": "snarkpack_aggregation/output"},
    "contracts": {
        "forge_out": "out",
        "deployed_contracts": "deployed_contracts.json",   # cdr_verifier + aggregate_anchor
        "proof_json": None,             # real snarkjs proof => exact verifier gas
        "public_json": None,
        "verifier_key_index": 2,        # first attester account; must not overlap mno_key_indices
        "deployer_key_index": 0,
        "mno_key_indices": [0, 1],
    },
    "execution": {
        "rate": 500.0,
        "workers": 16,
        "receipt_workers": 16,
        "receipt_timeout": 120.0,
        "poll_interval": 0.25,
        "latency_method": "block_time",
        "receipt_timeout_per_tx_s": 0.05,
        "drain_timeout_s": 900.0,
    },
    "monitoring": {"enabled": True, "interval": 1.0},
    "output": {"out_dir": "results/zkroam"},
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
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path) as f:
        user_cfg = yaml.safe_load(f) or {}
    cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)
    w, c, e = cfg["workload"], cfg["contracts"], cfg["execution"]

    args = SimpleNamespace(
        experiment_name=cfg["experiment"]["name"],
        topology=cfg["network"]["topology"],
        accounts=cfg["network"]["accounts"],
        sweep=cfg["sweep"]["nproofs"],
        runs=int(cfg["sweep"]["runs"]),
        mode=cfg["sweep"]["mode"],
        num_vmnos=int(w["num_vmnos"]),
        num_attesters=int(w["num_attesters"]),
        threshold=None if w["threshold"] is None else int(w["threshold"]),
        rates={"sms": int(w["rate_sms"]), "voice": int(w["rate_voice"]),
               "data": int(w["rate_data"])},
        margin_pct=int(w["estimate_margin_pct"]),
        setup_chunk=int(w["setup_chunk"]),
        ue_fund_wei=None if w["ue_fund_wei"] is None else int(w["ue_fund_wei"]),
        offchain_logs_dir=cfg["offchain"]["logs_dir"],
        forge_out=c["forge_out"],
        deployed_contracts=c["deployed_contracts"],
        proof_json=c["proof_json"], public_json=c["public_json"],
        verifier_key_index=int(c["verifier_key_index"]),
        deployer_key_index=int(c["deployer_key_index"]),
        mno_key_indices=[int(x) for x in c["mno_key_indices"]],
        rate=float(e["rate"]), workers=int(e["workers"]),
        receipt_workers=int(e["receipt_workers"]),
        receipt_timeout=float(e["receipt_timeout"]),
        poll_interval=float(e["poll_interval"]),
        latency_method=str(e["latency_method"]),
        receipt_timeout_per_tx=float(e["receipt_timeout_per_tx_s"]),
        drain_timeout_s=float(e["drain_timeout_s"]),
        monitor_resources=bool(cfg["monitoring"]["enabled"]),
        monitor_interval=float(cfg["monitoring"]["interval"]),
        out_dir=cfg["output"]["out_dir"],
    )
    args.sweep = ([int(x) for x in args.sweep.split(",") if str(x).strip()]
                  if isinstance(args.sweep, str) else [int(x) for x in args.sweep])

    if args.latency_method not in ("block_time", "poll"):
        raise ValueError("execution.latency_method must be block_time or poll")
    if not 1 <= args.num_attesters <= 50:
        raise ValueError("workload.num_attesters must be in [1, 50]")
    if args.threshold is not None and not 1 <= args.threshold <= args.num_attesters:
        raise ValueError("workload.threshold must be in [1, num_attesters]")
    if args.mode not in ("individual", "aggregate", "both"):
        raise ValueError("sweep.mode must be individual/aggregate/both")
    if len(args.mno_key_indices) != 2 or args.mno_key_indices[0] == args.mno_key_indices[1]:
        raise ValueError("contracts.mno_key_indices must be two distinct account indices")
    return args


# ============================================================
# Individual path: commit leg + terminateRoamingSession per session
# ============================================================

def probe_verifier_gas(env, args, ctx, pA, pB, pC, inputs):
    """Send ONE real verifyProof tx to the REAL verifier and return its gasUsed.

    Why a real tx and not estimate_gas: estimate_gas looks for the smallest gas at
    which the call does not revert, and a precompile that runs out of gas does not
    revert its caller. For a dummy proof that "minimum" is far below the real cost
    (the pairing never completes), and with BenchGroth16Verifier (which returns true
    regardless) a leg run with that limit would silently measure the wrong gas.
    The pairing alone costs >= 45000 + 4*34000, so anything cheaper means the
    verifier exited early (public input >= field order, invalid point)."""
    abi, _ = load_artifact(forge_artifact(args.forge_out, "CDRVerifier.sol", "Groth16Verifier"))
    data = env.web3s[0].eth.contract(abi=abi).encode_abi(
        abi_element_identifier="verifyProof",
        args=[list(pA), [list(pB[0]), list(pB[1])], list(pC), list(inputs)])
    if ctx.real_proof:
        # gas alone cannot tell a valid proof from a failed pairing (verifyProof
        # returns false, it does not revert), so check the result explicitly.
        vc = env.web3s[0].eth.contract(address=ctx.inner_verifier, abi=abi)
        ok = vc.functions.verifyProof(list(pA), [list(pB[0]), list(pB[1])],
                                      list(pC), list(inputs)).call()
        if not ok:
            raise RuntimeError("real proof does not verify against the given public "
                               "inputs: check public-signal order (circuit vs contract)")
    rc = _send_and_wait(env, ctx.deployer, {"to": ctx.inner_verifier, "gas": 1_000_000,
                                            "data": data})
    g = rc["gasUsed"]
    if g < 200_000:
        raise RuntimeError(
            f"verifyProof used only {g} gas: the verifier exits before the pairing "
            f"(public inputs must be < the BN254 field order and the points valid). "
            f"The individual leg would understate verifier gas.")
    print(f"verifier probe: real verifyProof tx used {g} gas (pairing executes)")
    return g


def run_individual(env, args, ctx, nproofs, run, row):
    n = nproofs * args.num_vmnos
    plan = session_plan(args.num_vmnos, nproofs, ctx.rates, args.margin_pct)
    if ctx.real_proof:
        # one real proof settles every session: all sessions must carry the
        # proof's cost and cdrHash (the verifier checks them as public inputs)
        p0 = ctx.real_proof[3][PS_COST]
        for p in plan:
            p["cost"] = p0
            p["est"] = max(p["est"], p0 + p0 * args.margin_pct // 100 + 1)
        hashes = [Web3.to_bytes(ctx.real_proof[3][PS_HASH]).rjust(32, b"\0")] * n
        pA, pB, pC = ctx.real_proof[0], ctx.real_proof[1], ctx.real_proof[2]
    else:
        hashes = [field_cdr_hash(p["cdr"]) for p in plan]
        pA, pB, pC = DUMMY_PA, DUMMY_PB, DUMMY_PC

    probe_gas = probe_verifier_gas(
        env, args, ctx, pA, pB, pC,
        pub_signals(ctx.rates, plan[0]["cost"], int.from_bytes(hashes[0], "big")))

    ue = ctx.ue_accts[:n]
    fund_ue_accounts(env, ctx.deployer, ue, ctx.ue_tx_budget, args.setup_chunk,
                     args.ue_fund_wei)
    env.phase("setup:individual")
    ag = deploy_agreement(env, "individual", ctx.deployer, ctx.mnos, ctx.rates,
                          ctx.token, args.forge_out, extra=ctx.verifier_address)
    sessions = start_sessions(env, ag, ctx.mnos, ue, plan, args.setup_chunk)
    env.phase("idle")

    commit = commit_leg(env, ag, ue, sessions, hashes,
                        f"individual_commit_n{nproofs}_vmnos{args.num_vmnos}_r{run}")

    senders = ctx.accounts
    specs = [{"sender": senders[u % len(senders)], "to": ag.address, "gas": 0,
              "data": ag.contract.encode_abi(
                  abi_element_identifier="terminateRoamingSession",
                  args=[sessions[u][0], list(pA), [list(pB[0]), list(pB[1])],
                        list(pC), plan[u]["cost"]])} for u in range(n)]
    # NOT estimate_gas alone: see probe_verifier_gas. The limit only has to be high
    # enough for the pairing to complete; unused gas is refunded, gasUsed is unaffected.
    gas = max(estimate_gas(env, senders[0]["address"], ag.address, specs[0]["data"], 1.15,
                           "terminateRoamingSession"),
              probe_gas + 400_000)
    check_gas_fits_block("individual", gas, env.web3s[0], "Verifier gas is too high.")
    for s in specs:
        s["gas"] = gas
    res = run_leg(env, f"individual_n{nproofs}_vmnos{args.num_vmnos}_r{run}", specs)
    settle_leg(env, res)

    row.update(leg_columns("individual_commit", commit))
    row.update(leg_columns("individual", res))
    cg, sg = row["individual_commit_total_gas_used"], row["individual_total_gas_used"]
    row["individual_contract"] = ag.address
    row["individual_verifier_mode"] = "real_proof" if ctx.real_proof else "bench_wrapper"
    row["individual_gas_per_session"] = sg / n if sg else None
    row["individual_e2e_gas_per_session"] = (cg + sg) / n if cg and sg else None
    row["individual_valid_all"] = bool(row["individual_commit_valid"] and row["individual_valid"])
    if (row["individual_gas_used_per_tx"] or 0) < probe_gas:
        # a settlement cannot cost less than the verifier call it contains
        print(f"  ERROR: individual gas/tx {row['individual_gas_used_per_tx']} < verifier "
              f"probe {probe_gas}: the pairing did not run, results are invalid")
        row["individual_valid_all"] = False
    row["individual_verifier_probe_gas"] = probe_gas
    return commit, res


# ============================================================
# Aggregate path: anchor leg (attest) + settleBatch leg. NO commit.
# ============================================================

def run_aggregate(env, args, ctx, nproofs, run, row, offchain, salt):
    n = nproofs * args.num_vmnos
    plan = session_plan(args.num_vmnos, nproofs, ctx.rates, args.margin_pct)
    ue = ctx.ue_accts[:n]            # UEs sign CDR hashes off-chain; they never send a tx

    env.phase("setup:aggregate")
    ag = deploy_agreement(env, "aggregate", ctx.deployer, ctx.mnos, ctx.rates,
                          ctx.token, args.forge_out, extra=ctx.anchor_address)
    sessions = start_sessions(env, ag, ctx.mnos, ue, plan, args.setup_chunk)
    env.phase("idle")

    proof_bytes = estimate_aggregate_proof_bytes(
        nproofs, offchain.get("aggregate_proof_bytes_measured"))
    domain = anchor_domain_separator(env.chain_id, ctx.anchor_address)

    # exactly t signatures per relay (signers stay sorted ascending: the slice of a sorted list)
    signers = ctx.attesters[:ctx.anchor_threshold]

    batches, relay_specs = [], []
    for g in range(args.num_vmnos):
        idx = range(g * nproofs, (g + 1) * nproofs)
        sids = [sessions[u][0] for u in idx]
        costs = [plan[u]["cost"] for u in idx]
        cdrs = [field_cdr_hash(plan[u]["cdr"]) for u in idx]
        batch_id = Web3.keccak(text=f"zkroam-batch-{salt}-n{nproofs}-r{run}-v{g}")
        # stand-in for the aggregate proof the verifiers checked off-chain
        agg_hash = Web3.keccak(make_calldata(proof_bytes))

        # UE signatures over (agreement, chainid, sessionId, cdrHash), packed r||vs per session
        ue_sigs = b"".join(
            sign_cdr_compact(ue_cdr_digest(ag.address, env.chain_id, sid, cdr),
                             ue[u]["private_key"])
            for u, sid, cdr in zip(idx, sids, cdrs))

        commitment = batch_commitment(ag.address, env.chain_id, batch_id, agg_hash,
                                      ctx.rates, sids, costs, cdrs)
        if g == 0:   # the python mirrors must equal the contract's own views
            onchain = ag.contract.functions.batchCommitment(
                batch_id, agg_hash, sids, costs, cdrs).call()
            if bytes(onchain) != bytes(commitment):
                raise RuntimeError("batch commitment mismatch between workload and contract")
            ue_dig = ag.contract.functions.ueCDRDigest(sids[0], cdrs[0]).call()
            if bytes(ue_dig) != ue_cdr_digest(ag.address, env.chain_id, sids[0], cdrs[0]):
                raise RuntimeError("UE digest mismatch between workload and contract")

        sid = settlement_id_of(ag.address, batch_id)
        digest = attestation_digest(domain, sid, commitment, nproofs)
        sigs = [sign_attestation(digest, a["private_key"]) for a in signers]
        calldata = build_relay_calldata_multi(
            env.web3s[0], sid, commitment, nproofs,
            [a["address"] for a in signers], sigs)
        relay_specs.append({"sender": ctx.accounts[g % len(ctx.accounts)],
                            "to": ctx.anchor_address, "data": calldata, "gas": 0})
        batches.append((batch_id, agg_hash, sids, costs, cdrs, ue_sigs))

    gas = estimate_gas(env, ctx.accounts[0]["address"], ctx.anchor_address,
                       relay_specs[0]["data"], 1.15, "relayAggregateProof")
    for s in relay_specs:
        s["gas"] = gas
    anchor = run_leg(env, f"aggregate_anchor_n{nproofs}_vmnos{args.num_vmnos}_r{run}", relay_specs)
    settle_leg(env, anchor)

    # Negative test (eth_call, no state change): settleBatch must reject swapped UE signatures.
    # The anchor leg has finalized the batch, so a revert here can only come from the UE check.
    b0 = batches[0]
    if nproofs >= 2:
        swapped = b0[5][64:128] + b0[5][:64] + b0[5][128:]
        accepted = False
        try:
            ag.contract.functions.settleBatch(swapped, *b0[:5]).call(
                {"from": ctx.accounts[0]["address"]})
            accepted = True
        except Exception:
            pass
        if accepted:
            raise RuntimeError("settleBatch accepted tampered UE signatures")
        print("  UE-signature negative test passed (swapped signatures rejected)")

    settle_specs = [{"sender": ctx.accounts[g % len(ctx.accounts)], "to": ag.address, "gas": 0,
                     "data": ag.contract.encode_abi(
                         abi_element_identifier="settleBatch",
                         args=[b[5], b[0], b[1], b[2], b[3], b[4]])}
                    for g, b in enumerate(batches)]
    sgas = estimate_gas(env, ctx.accounts[0]["address"], ag.address,
                        settle_specs[0]["data"], 1.15, "settleBatch")
    check_gas_fits_block("aggregate_settle", sgas, env.web3s[0],
                         "A batch of nproofs sessions must fit in one block: "
                         "lower sweep.nproofs or raise the block gas limit.")
    for s in settle_specs:
        s["gas"] = sgas
    settle = run_leg(env, f"aggregate_settle_n{nproofs}_vmnos{args.num_vmnos}_r{run}", settle_specs)
    settle_leg(env, settle)

    row.update(leg_columns("aggregate_anchor", anchor))
    row.update(leg_columns("aggregate_settle", settle))
    ag_gas, st_gas = row["aggregate_anchor_total_gas_used"], row["aggregate_settle_total_gas_used"]
    total = (ag_gas + st_gas) if ag_gas and st_gas else None
    d_a, d_s = row["aggregate_anchor_confirm_duration_s"], row["aggregate_settle_confirm_duration_s"]
    row.update({
        "aggregate_contract": ag.address,
        "aggregate_num_attesters": len(signers),          # signatures relayed per batch (= t)
        "aggregate_committee_size": args.num_attesters,   # provisioned committee m
        "anchor_threshold": ctx.anchor_threshold,
        "aggregate_proof_bytes": proof_bytes,
        "aggregate_proof_bytes_is_measured": bool(offchain.get("aggregate_proof_bytes_measured")),
        "aggregate_total_tx": len(anchor.txs) + len(settle.txs),
        "aggregate_total_gas_used": total,
        "aggregate_gas_per_vmno_batch": total / args.num_vmnos if total else None,
        "aggregate_gas_per_session": total / n if total else None,
        "aggregate_e2e_gas_per_session": total / n if total else None,   # no commit leg; UE sig is inside settle
        "aggregate_confirm_duration_s": (d_a or 0) + (d_s or 0),
        "aggregate_valid_all": bool(row["aggregate_anchor_valid"] and row["aggregate_settle_valid"]),
    })
    return anchor, settle


# ============================================================
# Sweep driver
# ============================================================

def main():
    cli = argparse.ArgumentParser(
        description="zkRoam workload against the real Agreement contracts: "
                    "individual-proof vs SnarkPack-aggregate settlement.")
    cli.add_argument("--config", default="config.yml")
    args = load_config(cli.parse_args().config)

    os.makedirs(args.out_dir, exist_ok=True)
    detail_dir = os.path.join(args.out_dir, "detail")
    os.makedirs(detail_dir, exist_ok=True)

    monitor = None
    if args.monitor_resources:
        monitor = ResourceMonitor(args.monitor_interval)
        monitor.start()
        monitor.set_phase("setup:connecting")

    topology = load_json(args.topology)
    endpoints = build_rpc_endpoints(topology)
    web3s, chain_id = connect_web3(endpoints)
    accounts = load_accounts(args.accounts)
    if not accounts:
        raise RuntimeError("No workload accounts found.")
    assignment = assign_accounts(accounts, endpoints)
    env = Env(web3s, chain_id, accounts, assignment, endpoints, args)
    env.monitor = monitor

    deployed = load_deployed_contracts(args.deployed_contracts) or {}
    need_ind = args.mode in ("individual", "both")
    need_agg = args.mode in ("aggregate", "both")

    def addr_of(key):
        if deployed.get(key):
            return Web3.to_checksum_address(deployed[key]["address"])
        return None

    verifier = addr_of("cdr_verifier")
    anchor_address = addr_of("aggregate_anchor")
    if need_ind and not verifier:
        raise RuntimeError("deployed_contracts.json has no 'cdr_verifier' "
                           "(run deploy_verifiers.py first)")
    if need_agg and not anchor_address:
        raise RuntimeError("deployed_contracts.json has no 'aggregate_anchor' "
                           "(run deploy_verifiers.py first)")

    real_proof = None
    rates = dict(args.rates)
    if args.proof_json and args.public_json:
        real_proof = load_snarkjs_proof(args.proof_json, args.public_json)
        ps = real_proof[3]
        rates = {"sms": ps[PS_RSMS], "data": ps[PS_RDATA], "voice": ps[PS_RVOICE]}   # circuit public-signal order
        print(f"\nUsing real proof from {args.proof_json}: rates fixed by its public inputs {rates}")
    elif need_ind:
        print("\nNo real proof given: 'individual' settles through BenchGroth16Verifier, which "
              "runs the real pairing check (same precompile gas) and returns true. It adds one "
              "extra STATICCALL (~3k gas) per settlement. Set contracts.proof_json/public_json "
              "for exact numbers.")

    deployer = accounts[args.deployer_key_index]
    mnos = [accounts[i] for i in args.mno_key_indices]
    token = Token(env, deployer, args.forge_out)
    for m in mnos:
        token.mint(env, deployer, m["address"], 10 ** 30)

    verifier_address = verifier
    if need_ind and not real_proof:
        babi, bbc = load_artifact(forge_artifact(args.forge_out, "BenchGroth16Verifier.sol",
                                                 "BenchGroth16Verifier"))
        verifier_address, _ = deploy_contract(env, deployer, babi, bbc, verifier)
        print(f"bench verifier wrapper: {verifier_address} (inner {verifier})")

    attesters, anchor_threshold = None, None
    if need_agg:
        print(f"\nprovisioning {args.num_attesters} attesters on the anchor...")
        attesters = pick_attesters(accounts, args.verifier_key_index, args.num_attesters)
        anchor_threshold = ensure_attesters(web3s[0], chain_id, anchor_address, accounts,
                                            attesters, args.threshold)
        env._nonce.clear()    # admin txs consumed nonces
        if anchor_threshold > len(attesters):
            raise RuntimeError(
                f"anchor threshold {anchor_threshold} > committee size {len(attesters)}: "
                f"no batch could ever finalize. Set workload.threshold <= num_attesters.")
        overlap = {a["address"].lower() for a in attesters} & {m["address"].lower() for m in mnos}
        if overlap:
            raise RuntimeError(
                "an attester account is also an MNO account (a VMNO would attest its own "
                "batch). Raise contracts.verifier_key_index so attesters are distinct from "
                f"contracts.mno_key_indices; overlapping: {sorted(overlap)}")

    max_n = max(args.sweep) * args.num_vmnos
    print(f"generating {max_n} UE accounts...")
    ctx = SimpleNamespace(
        accounts=accounts, deployer=deployer, mnos=mnos, token=token, rates=rates,
        real_proof=real_proof, verifier_address=verifier_address, inner_verifier=verifier,
        anchor_address=anchor_address, attesters=attesters,
        anchor_threshold=anchor_threshold,
        ue_accts=make_ue_accounts(max_n, f"zkroam-{int(time.time())}", len(web3s)),
        ue_tx_budget=args.runs * len(args.sweep) + 2,
    )
    if monitor is not None:
        monitor.set_phase("idle")

    print(f"\nworkload.num_vmnos = {args.num_vmnos} (nproofs below is the per-VMNO batch size)")
    salt = secrets.token_hex(4)
    rows = []
    for nproofs in args.sweep:
        for run in range(1, args.runs + 1):
            offchain = load_offchain_log(args.offchain_logs_dir, nproofs, run)
            proof_ms_max = offchain.get("proof_ms_max", offchain.get("proof_ms"))
            print("\n" + "=" * 60)
            print(f"nproofs/vmno={nproofs}  num_vmnos={args.num_vmnos}  run={run}  "
                  f"(off-chain source: {offchain['source']})")
            print("=" * 60)
            agg_ser = offchain.get("agg_serialize_ms")
            agg_work = offchain["agg_ms"] + offchain["agg_verify_ms"] + (agg_ser or 0)
            row = {
                "nproofs_per_vmno": nproofs, "num_vmnos": args.num_vmnos, "run": run,
                "sessions_total": nproofs * args.num_vmnos,
                "offchain_source": offchain["source"],
                "offchain_proof_ms_max": proof_ms_max,
                "offchain_proof_ms_avg": offchain.get("proof_ms_avg"),
                "offchain_proof_ms_total": offchain.get("proof_ms_total"),
                "offchain_param_ms": offchain.get("param_ms"),
                "offchain_single_verify_ms": offchain.get("single_verify_ms"),
                "offchain_agg_ms": offchain["agg_ms"],
                "offchain_agg_serialize_ms": agg_ser,
                "offchain_agg_verify_ms": offchain["agg_verify_ms"],
                "offchain_peak_mb": offchain["peak_mb"],
                "offchain_total_experiment_ms": offchain.get("total_experiment_ms"),
                "offchain_proof_work_ms_all_vmnos": (
                    offchain["proof_ms_total"] * args.num_vmnos
                    if offchain.get("proof_ms_total") is not None else None),
                "offchain_aggregate_work_ms_all_vmnos": agg_work * args.num_vmnos,
            }

            if need_ind:
                commit, res = run_individual(env, args, ctx, nproofs, run, row)
                if proof_ms_max is not None and row["individual_confirm_duration_s"] is not None:
                    row["individual_pipeline_latency_s"] = (
                        proof_ms_max / 1000.0 + row["individual_confirm_duration_s"])
                write_leg_detail(detail_dir, "individual_commit", nproofs, args, run,
                                 commit.txs, endpoints)
                write_leg_detail(detail_dir, "individual", nproofs, args, run, res.txs, endpoints)

            if need_agg:
                anchor, settle = run_aggregate(env, args, ctx, nproofs, run, row, offchain, salt)
                if proof_ms_max is not None:
                    row["aggregate_pipeline_latency_s"] = (
                        proof_ms_max / 1000.0 + offchain["agg_ms"] / 1000.0
                        + (agg_ser or 0) / 1000.0 + offchain["agg_verify_ms"] / 1000.0
                        + row["aggregate_confirm_duration_s"])
                write_leg_detail(detail_dir, "aggregate_anchor", nproofs, args, run,
                                 anchor.txs, endpoints)
                write_leg_detail(detail_dir, "aggregate_settle", nproofs, args, run,
                                 settle.txs, endpoints)

            # ---- head-to-head ----
            ind_s, agg_t = row.get("individual_total_gas_used"), row.get("aggregate_total_gas_used")
            if ind_s and agg_t:
                # settlement only: terminateRoamingSession x N  vs  anchor + settleBatch
                row["gas_savings_pct"] = (1 - agg_t / ind_s) * 100
            i_lat, a_lat = row.get("individual_pipeline_latency_s"), row.get("aggregate_pipeline_latency_s")
            if i_lat and a_lat:
                row["pipeline_speedup_x"] = i_lat / a_lat
            # end to end: individual pays commit + settle; aggregate has NO commit leg
            i_c = row.get("individual_commit_total_gas_used")
            if i_c and ind_s:
                row["e2e_individual_total_gas"] = i_c + ind_s
                row["e2e_individual_gas_per_session"] = (i_c + ind_s) / row["sessions_total"]
                row["commit_share_of_individual_e2e_pct"] = 100.0 * i_c / (i_c + ind_s)
            if agg_t:
                row["e2e_aggregate_total_gas"] = agg_t
                row["e2e_aggregate_gas_per_session"] = agg_t / row["sessions_total"]
            if row.get("e2e_individual_total_gas") and agg_t:
                row["e2e_gas_savings_pct"] = (1 - agg_t / row["e2e_individual_total_gas"]) * 100
            row["all_legs_valid"] = all(row.get(k) for k in
                                        ("individual_valid_all", "aggregate_valid_all")
                                        if k in row)
            rows.append(row)

    stem = (f"{args.experiment_name}_{args.num_vmnos}_{args.rate}_{args.workers}_"
            f"{args.receipt_workers}")
    if monitor is not None:
        monitor.stop()
        monitor.write_csv(os.path.join(args.out_dir, f"{stem}_resource_usage.csv"))
    write_sweep_summary(os.path.join(args.out_dir, f"{stem}_sweep_summary.csv"), rows)
    print_sweep_highlights(rows)


# ============================================================
# Output writers
# ============================================================

def write_leg_detail(detail_dir, label, nproofs, args, run, transactions, endpoints):
    if transactions is None:
        return
    path = os.path.join(detail_dir, f"{label}_n{nproofs}_{args.num_vmnos}_{args.rate}_"
                                    f"{args.workers}_{args.receipt_workers}_r{run}.csv")
    fields = ["tx_index", "sender", "rpc_node", "tx_hash", "nonce", "submit_ts", "confirm_ts",
              "latency_s", "poll_latency_s", "block_number", "block_ts", "gas_used",
              "status", "error"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in transactions:
            poll = t.get("poll_latency_s")
            if poll is None and t["submit_ts"] and t["confirm_ts"]:
                poll = t["confirm_ts"] - t["submit_ts"]
            lat = t.get("latency_s")
            w.writerow({
                "tx_index": t["tx_index"], "sender": t["sender"],
                "rpc_node": endpoints[t["rpc_index"]]["name"], "tx_hash": t["tx_hash"],
                "nonce": t["nonce"], "submit_ts": t["submit_ts"], "confirm_ts": t["confirm_ts"],
                "latency_s": lat if lat is not None else poll, "poll_latency_s": poll,
                "block_number": t["block_number"], "block_ts": t.get("block_ts"),
                "gas_used": t.get("gas_used"), "status": t["status"], "error": t["error"],
            })


def write_sweep_summary(path, rows):
    if not rows:
        return
    fields = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, restval="")
        w.writeheader()
        w.writerows({k: ("" if v is None else v) for k, v in r.items()} for r in rows)
    print(f"\nWrote: {path}")


def print_sweep_highlights(rows):
    if not rows:
        return
    by_n = defaultdict(list)
    for r in rows:
        by_n[r["nproofs_per_vmno"]].append(r)

    def med(rs, k):
        v = [r[k] for r in rs if r.get(k) is not None]
        return statistics.median(v) if v else 0

    print("\n" + "=" * 118)
    print("SWEEP HIGHLIGHTS (median across runs per nproofs/VMNO level)")
    print("=" * 118)
    print(f"{'nproofs':>8} {'vmnos':>6} {'ind_settle_gas':>15} {'agg_total_gas':>14} "
          f"{'settle_save_%':>14} {'e2e_ind/sess':>13} {'e2e_agg/sess':>13} "
          f"{'e2e_save_%':>11} {'pipeline_x':>11}")
    for n in sorted(by_n):
        rs = by_n[n]
        print(f"{n:>8} {rs[0]['num_vmnos']:>6} {med(rs, 'individual_total_gas_used'):>15,.0f} "
              f"{med(rs, 'aggregate_total_gas_used'):>14,.0f} {med(rs, 'gas_savings_pct'):>13.1f}% "
              f"{med(rs, 'e2e_individual_gas_per_session'):>13,.0f} "
              f"{med(rs, 'e2e_aggregate_gas_per_session'):>13,.0f} "
              f"{med(rs, 'e2e_gas_savings_pct'):>10.1f}% {med(rs, 'pipeline_speedup_x'):>10.2f}x"
              + ("" if all(r.get("all_legs_valid", True) for r in rs) else "  [INVALID: failed txs]"))
    print("=" * 118)


if __name__ == "__main__":
    main()