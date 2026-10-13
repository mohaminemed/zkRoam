#!/usr/bin/env python3
"""
plain_workload.py

BASELINE workload for the zkRoam benchmark: settle roaming sessions on
chain PLAINLY (full CDR in calldata, billing logic re-executed by the
contract, no ZK proof, no aggregation) so the individual-proof and
SnarkPack-aggregate legs of zkroam_workload.py have something to be
compared against. Contract: PlainRoamingSettlement.sol.

Same harness, same sweep, same workload knobs as zkroam_workload.py:

  sweep.nproofs      -> sessions (CDRs) ONE VMNO settles together
  workload.num_vmnos -> how many VMNO settlements are submitted

Legs (sweep.mode: stateless | stateful | batch | all):

  stateless  nproofs * num_vmnos txs, each calling verifyPlain(cdr).
             View function, no storage: the direct counterpart of the
             "individual" leg's verifyProof() call.
  stateful   nproofs * num_vmnos txs, each calling settlePlain(cdr):
             validation + replay guard + record + accumulate amount owed.
             The honest "individual" baseline, since it persists results.
  batch      num_vmnos * ceil(nproofs / batch_chunk) txs, each calling
             settleBatch(cdrs[chunk]). The plain alternative to
             aggregation: ONE tx per VMNO when nproofs <= batch_chunk,
             at the price of calldata that grows linearly in nproofs.

There is no off-chain proving or aggregation step, so the pipeline
latency of a plain leg is just its on-chain makespan
(last confirmation - first submission). Compare it with
individual_pipeline_latency_s / aggregate_pipeline_latency_s.

LATENCY MEASUREMENT
------------------------------------------------------------------
Per-tx latency = (timestamp of the block that mined the tx) - (that
tx's OWN submit_ts). The receipt poller only starts after submit_all()
returns, so confirm_ts - submit_ts would also count the time spent
submitting the rest of the batch plus the poll sweep. Set
execution.latency_method: poll to get the old behaviour back.
Block timestamps are whole seconds, so sub-second values are only
accurate to about +-1 s, and client/node clocks should be NTP-synced.

CONTRACT SETUP
------------------------------------------------------------------
The contract address is read from deployed_contracts.json under
"plain_settlement": {"address": "0x..."}. If the key is absent and
contracts.plain_artifact points at a compiled artifact (JSON with "abi"
and "bytecode", Hardhat or Foundry layout), the script deploys it from
accounts[deployer_key_index] and writes the address back. Tariffs for
the operator pairs used are then set if missing; that needs the owner
key, so the deployer must be accounts[deployer_key_index] (default 0).

Compile with e.g.:  forge build   or   solc --abi --bin PlainRoamingSettlement.sol

ASSUMPTIONS
------------------------------------------------------------------
1. The CDR layout and billing rule are a stand-in for what the real
   CDR circuit proves (see the contract header). Swap in the real rule.
2. Session ids are salted per process run, so repeated runs against the
   same chain never trip the contract's replay guard.
3. After every leg the script waits for the txpool to drain and the
   next leg re-reads nonces from the chain, so a congested or failed
   leg cannot leave a backlog or nonce gap that stalls the next one.
4. gas used is read from receipts, not estimated, whenever the
   contract is deployed (always the case here).
------------------------------------------------------------------
"""

import argparse
import csv
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
from web3 import Web3

from besu_benchmark import (
    ResourceMonitor,
    build_rpc_endpoints,
    connect_web3,
    load_accounts,
    assign_accounts,
    load_nonces,
    submit_all,
    poll_all_receipts,
    compute_statistics,
    load_json,
)


# ============================================================
# ABI (hand-written so the script works without an artifact when
# the contract is already deployed)
# ============================================================

CDR_COMPONENTS = [
    {"name": "sessionId", "type": "bytes32"},
    {"name": "vmno", "type": "uint32"},
    {"name": "hmno", "type": "uint32"},
    {"name": "startTime", "type": "uint64"},
    {"name": "endTime", "type": "uint64"},
    {"name": "volumeKB", "type": "uint64"},
    {"name": "charge", "type": "uint128"},
]

PLAIN_ABI = [
    {"type": "function", "name": "verifyPlain", "stateMutability": "view",
     "inputs": [{"name": "c", "type": "tuple", "components": CDR_COMPONENTS}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "settlePlain", "stateMutability": "nonpayable",
     "inputs": [{"name": "c", "type": "tuple", "components": CDR_COMPONENTS}],
     "outputs": []},
    {"type": "function", "name": "settleBatch", "stateMutability": "nonpayable",
     "inputs": [{"name": "cdrs", "type": "tuple[]", "components": CDR_COMPONENTS}],
     "outputs": []},
    {"type": "function", "name": "setTariff", "stateMutability": "nonpayable",
     "inputs": [{"name": "vmno", "type": "uint32"},
                {"name": "hmno", "type": "uint32"},
                {"name": "ratePerSecond", "type": "uint64"},
                {"name": "ratePerKB", "type": "uint64"}],
     "outputs": []},
    {"type": "function", "name": "tariffs", "stateMutability": "view",
     "inputs": [{"name": "", "type": "uint64"}],
     "outputs": [{"name": "ratePerSecond", "type": "uint64"},
                 {"name": "ratePerKB", "type": "uint64"},
                 {"name": "active", "type": "bool"}]},
    {"type": "function", "name": "owner", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]

MODES = ("stateless", "stateful", "batch")


# ============================================================
# Contract wiring
# ============================================================

def load_artifact(path):
    art = load_json(path)
    abi = art["abi"]
    bc = art["bytecode"]
    if isinstance(bc, dict):          # Foundry: {"object": "0x..."}
        bc = bc["object"]
    if not str(bc).startswith("0x"):
        bc = "0x" + bc
    return abi, bc


def send_and_wait(w3, account, tx, timeout=120):
    tx = dict(tx)
    tx.setdefault("nonce", w3.eth.get_transaction_count(account["address"], "pending"))
    tx.setdefault("gasPrice", w3.eth.gas_price)
    signed = w3.eth.account.sign_transaction(tx, account["private_key"])
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
    if rc["status"] != 1:
        raise RuntimeError(f"admin tx reverted: {h.hex()}")
    return rc


def deploy_plain(w3, account, chain_id, artifact_path):
    abi, bytecode = load_artifact(artifact_path)
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    data = contract.constructor().data_in_transaction
    gas = int(w3.eth.estimate_gas({"from": account["address"], "data": data}) * 1.2)
    rc = send_and_wait(w3, account, {
        "chainId": chain_id, "to": None, "value": 0, "gas": gas, "data": data,
    })
    return Web3.to_checksum_address(rc["contractAddress"])


def resolve_contract(args, w3, chain_id, accounts):
    """-> checksum address of the PlainRoamingSettlement contract."""
    deployed = {}
    if args.deployed_contracts and os.path.exists(args.deployed_contracts):
        deployed = load_json(args.deployed_contracts)

    entry = deployed.get("plain_settlement")
    if entry and entry.get("address"):
        addr = Web3.to_checksum_address(entry["address"])
        if w3.eth.get_code(addr) not in (b"", b"\x00"):
            print(f"plain contract: using {addr} from {args.deployed_contracts}")
            return addr
        print(f"plain contract: {addr} has no code on this chain, redeploying")

    if not args.plain_artifact:
        raise RuntimeError(
            "No deployed 'plain_settlement' found and contracts.plain_artifact "
            "is not set. Compile PlainRoamingSettlement.sol and set "
            "contracts.plain_artifact, or add the address to "
            f"{args.deployed_contracts} as "
            '{"plain_settlement": {"address": "0x..."}}.'
        )

    deployer = accounts[args.deployer_key_index]
    addr = deploy_plain(w3, deployer, chain_id, args.plain_artifact)
    print(f"plain contract: deployed at {addr} by {deployer['address']}")

    deployed["plain_settlement"] = {"address": addr}
    with open(args.deployed_contracts, "w") as f:
        json.dump(deployed, f, indent=2)
    return addr


def operator_pairs(num_operators):
    """(vmno, hmno) pairs used by the workload: vmno k bills hmno k % K + 1."""
    return [(v, v % num_operators + 1) for v in range(1, num_operators + 1)]


def ensure_tariffs(w3, contract, chain_id, owner_account, pairs, rps, rpk):
    owner = contract.functions.owner().call()
    if owner.lower() != owner_account["address"].lower():
        print(f"WARNING: contract owner is {owner}, not accounts"
              f"[deployer_key_index]; setTariff will revert unless tariffs "
              f"are already set.")
    for vmno, hmno in pairs:
        key = (vmno << 32) | hmno
        cur = contract.functions.tariffs(key).call()
        if cur[2] and cur[0] == rps and cur[1] == rpk:
            continue
        data = contract.encode_abi(
            abi_element_identifier="setTariff", args=[vmno, hmno, rps, rpk]
        )
        gas = int(w3.eth.estimate_gas({
            "from": owner_account["address"], "to": contract.address, "data": data,
        }) * 1.2)
        send_and_wait(w3, owner_account, {
            "chainId": chain_id, "to": contract.address, "value": 0,
            "gas": gas, "data": data,
        })
        print(f"  tariff set: vmno={vmno} hmno={hmno} "
              f"ratePerSecond={rps} ratePerKB={rpk}")


# ============================================================
# CDR generation
# ============================================================

def make_cdr(salt, tag, nproofs, run, vmno_index, session_index, num_operators,
             base_ts, rps, rpk):
    """Deterministic, valid CDR. Returns the struct as a tuple in ABI order.

    `tag` (the leg's mode) is part of the session id: legs that persist state
    (stateful, batch) must never reuse another leg's session ids, or the
    contract's replay guard reverts them with AlreadySettled."""
    vmno = 1 + vmno_index % num_operators
    hmno = vmno % num_operators + 1
    session_id = Web3.keccak(
        text=f"plain-{salt}-{tag}-n{nproofs}-r{run}-v{vmno_index}-s{session_index}"
    )
    start = base_ts + session_index * 3
    duration = 30 + (session_index * 37 + vmno_index * 11) % 3570   # < 1 day
    end = start + duration
    volume = 1000 + (session_index * 7919 + vmno_index * 104729) % 500_000
    charge = duration * rps + volume * rpk
    return (session_id, vmno, hmno, start, end, volume, charge)


def encode_call(contract, fn, arg):
    return Web3.to_bytes(hexstr=contract.encode_abi(
        abi_element_identifier=fn, args=[arg]
    ))


def build_leg_calldata(contract, mode, salt, nproofs, run, num_vmnos,
                       num_operators, batch_chunk, base_ts, rps, rpk):
    """-> list of calldata bytes, one per transaction of this leg."""
    calldata = []
    for v in range(num_vmnos):
        cdrs = [make_cdr(salt, mode, nproofs, run, v, i, num_operators, base_ts, rps, rpk)
                for i in range(nproofs)]
        if mode == "batch":
            for k in range(0, len(cdrs), batch_chunk):
                calldata.append(encode_call(contract, "settleBatch", cdrs[k:k + batch_chunk]))
        else:
            fn = "verifyPlain" if mode == "stateless" else "settlePlain"
            for c in cdrs:
                calldata.append(encode_call(contract, fn, c))
    return calldata


# ============================================================
# Transactions
# ============================================================

def prepare_transactions(accounts, assignment, nonces, web3s, chain_id,
                         calldata_list, gas_per_tx, to_address):
    next_nonce = dict(nonces)
    gas_price = web3s[0].eth.gas_price      # once per leg, not once per tx
    txs = []
    for i, data in enumerate(calldata_list):
        acc = accounts[i % len(accounts)]
        ai = acc["index"]
        rpc_index = assignment[ai]
        nonce = next_nonce[ai]
        next_nonce[ai] += 1
        signed = web3s[rpc_index].eth.account.sign_transaction({
            "chainId": chain_id, "nonce": nonce, "to": to_address, "value": 0,
            "gas": gas_per_tx, "gasPrice": gas_price, "data": Web3.to_hex(data),
        }, acc["private_key"])
        txs.append({
            "tx_index": i, "account_index": ai, "sender": acc["address"],
            "rpc_index": rpc_index, "raw": signed.raw_transaction, "nonce": nonce,
            "tx_hash": None, "submit_ts": None, "confirm_ts": None,
            "block_number": None, "status": "prepared", "error": None,
            "gas_used": None,
        })
    return txs


def fetch_gas_used(txs, web3s, workers=16):
    def one(t):
        try:
            rc = web3s[t["rpc_index"]].eth.get_transaction_receipt(t["tx_hash"])
            t["gas_used"] = rc["gasUsed"]
        except Exception:  # noqa: BLE001
            t["gas_used"] = None

    todo = [t for t in txs if t["status"] == "confirmed" and t["tx_hash"]]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, todo))
    return [t["gas_used"] for t in todo if t["gas_used"] is not None]


def latest_block_gas_limit(w3):
    """Block gas limit via raw RPC. web3.py's get_block() validates extraData
    length and raises ExtraDataLengthError on QBFT/IBFT chains (the header
    carries the validator set and seals), so avoid it."""
    res = w3.provider.make_request("eth_getBlockByNumber", ["latest", False])
    if res.get("error"):
        raise RuntimeError(f"eth_getBlockByNumber failed: {res['error']}")
    return int(res["result"]["gasLimit"], 16)


def block_timestamp(w3, block_number):
    """Block timestamp via raw RPC (same QBFT extraData reason as above)."""
    res = w3.provider.make_request("eth_getBlockByNumber", [hex(block_number), False])
    if res.get("error") or not res.get("result"):
        return None
    return int(res["result"]["timestamp"], 16)


def estimate_gas_limit(w3, sender, to_address, data, margin=1.15):
    """estimate_gas on a valid CDR must succeed. A failure means the call would
    revert (e.g. AlreadySettled, missing tariff, bad ABI encoding), so abort
    instead of submitting a whole leg of doomed transactions."""
    try:
        g = w3.eth.estimate_gas({
            "from": sender, "to": to_address, "data": Web3.to_hex(data),
        })
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"estimate_gas failed - the call would revert: {e}\n"
            f"Check the contract state (AlreadySettled? tariff set?) and the ABI."
        ) from e
    return int(g * margin)


# ============================================================
# Latency: block time instead of receipt-seen time
# ============================================================

def apply_block_time_latency(transactions, web3s, method="block_time"):
    """
    Per-tx latency = (timestamp of the block that mined the tx) - (that
    tx's own submit_ts). Receipt polling starts only after submit_all()
    returns, so confirm_ts - submit_ts also contains the time spent
    submitting the rest of the batch plus the poll sweep.

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
                except Exception:  # noqa: BLE001
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


# ============================================================
# Settling between legs
# ============================================================

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
        except Exception:  # noqa: BLE001
            return 0

    start = time.time()
    while True:
        with ThreadPoolExecutor(max_workers=16) as ex:
            remaining = sum(ex.map(backlog, accounts))
        if remaining == 0:
            return True, time.time() - start, 0
        if time.time() - start > timeout_s:
            return False, time.time() - start, remaining
        time.sleep(poll_s)


def settle_network(label, accounts, assignment, web3s, timeout_s,
                   resource_monitor=None):
    """
    Called after every leg so one leg's backlog (e.g. thousands of
    stateful txs at 500 tx/s) cannot congest the next one. Nonces are
    re-read from the chain at the start of the next leg (run_leg).
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

    if resource_monitor is not None:
        resource_monitor.set_phase("idle")


# ============================================================
# Leg metrics
# ============================================================

def leg_makespan(txs):
    c = [t for t in txs if t["status"] == "confirmed"
         and t["submit_ts"] and t["confirm_ts"]]
    if not c:
        return None
    return max(t["confirm_ts"] for t in c) - min(t["submit_ts"] for t in c)


def block_usage(txs):
    blocks = [t["block_number"] for t in txs
              if t["status"] == "confirmed" and t["block_number"] is not None]
    if not blocks:
        return None, None
    return len(set(blocks)), max(blocks) - min(blocks) + 1


def leg_metrics(prefix, txs, stats, gas_used_vals, sessions_per_leg, lat):
    n_blocks, span = block_usage(txs)
    total_gas = sum(gas_used_vals) if gas_used_vals else None
    return {
        f"{prefix}_total_tx": len(txs),
        f"{prefix}_confirmed": stats["confirmed"],
        f"{prefix}_reverted": stats["reverted"],
        f"{prefix}_timeout": stats["timeout"],
        f"{prefix}_success_rate_pct": stats["success_rate_pct"],
        f"{prefix}_submit_throughput_tx_s": stats["submission_throughput_tx_s"],
        f"{prefix}_confirm_throughput_tx_s": stats["confirmation_throughput_tx_s"],
        # per-tx latency from the tx's own submission to its mining block
        f"{prefix}_latency_avg_s": lat["mean"],
        f"{prefix}_latency_p50_s": lat["p50"],
        f"{prefix}_latency_p99_s": lat["p99"],
        f"{prefix}_makespan_s": leg_makespan(txs),
        f"{prefix}_blocks_used": n_blocks,
        f"{prefix}_block_span": span,
        f"{prefix}_gas_used_per_tx": (statistics.median(gas_used_vals)
                                      if gas_used_vals else None),
        f"{prefix}_total_gas_used": total_gas,
        f"{prefix}_gas_per_session": (total_gas / sessions_per_leg
                                      if total_gas and sessions_per_leg else None),
        # No off-chain proving/aggregation: pipeline == on-chain makespan.
        f"{prefix}_pipeline_latency_s": leg_makespan(txs),
    }


# ============================================================
# One on-chain leg
# ============================================================

def run_leg(label, mode, accounts, assignment, web3s, endpoints, chain_id,
            contract, calldata_list, args, resource_monitor):

    print()
    sizes = sorted({len(c) for c in calldata_list})
    print(f">>> plain leg: {label}  ({len(calldata_list)} tx, "
          f"calldata {sizes[0]}..{sizes[-1]} B/tx)")

    gas = estimate_gas_limit(
        web3s[0], accounts[0]["address"], contract.address, calldata_list[0]
    )
    # For batch legs calldata_list[0] is a full chunk, i.e. the largest tx.
    block_gas_limit = latest_block_gas_limit(web3s[0])
    if gas > 0.9 * block_gas_limit:
        raise RuntimeError(
            f"{label}: estimated {gas} gas/tx exceeds 90% of the block gas "
            f"limit ({block_gas_limit}). Lower workload.batch_chunk."
        )
    print(f"    gas limit per tx = {gas} (block gas limit {block_gas_limit})")

    # Fresh nonces: a failed earlier leg must not leave a gap.
    nonces = load_nonces(accounts, assignment, web3s)

    txs = prepare_transactions(accounts, assignment, nonces, web3s, chain_id,
                               calldata_list, gas, contract.address)

    # Large legs can legitimately need far longer than a flat timeout.
    receipt_timeout = max(args.receipt_timeout,
                          len(txs) * args.receipt_timeout_per_tx)

    if resource_monitor is not None:
        resource_monitor.set_phase(f"{label}:submission")
    submit_all(txs, web3s, target_rate=args.rate, workers=args.workers)

    if resource_monitor is not None:
        resource_monitor.set_phase(f"{label}:confirmation")
    poll_all_receipts(txs, web3s, workers=args.receipt_workers,
                      timeout=receipt_timeout,
                      poll_interval=args.poll_interval)
    if resource_monitor is not None:
        resource_monitor.set_phase("idle")

    stats = compute_statistics(txs)
    gas_used = fetch_gas_used(txs, web3s)
    lat = apply_block_time_latency(txs, web3s, args.latency_method)

    if stats["reverted"] or stats["timeout"] or stats["failed_submission"]:
        print(f"  WARNING {label}: reverted={stats['reverted']} "
              f"timeout={stats['timeout']} "
              f"failed_submission={stats['failed_submission']}")
    return txs, stats, gas_used, lat


# ============================================================
# Config
# ============================================================

DEFAULT_CONFIG = {
    "experiment": {"name": "plain_baseline"},
    "network": {
        "topology": "networkFiles/topology.json",
        "accounts": "accounts/accounts.json",
    },
    "sweep": {
        "nproofs": [8],
        "runs": 1,
        "mode": "all",   # stateless | stateful | batch | all
    },
    "workload": {
        "num_vmnos": 8,
        # distinct operator ids used for CDRs (>= 2); tariffs are set for each
        "num_operators": 8,
        # max CDRs per settleBatch tx; lower it if the gas guard trips
        "batch_chunk": 50,
        "rate_per_second": 3,
        "rate_per_kb": 2,
    },
    "contracts": {
        "deployed_contracts": "deployed_contracts.json",
        "plain_artifact": None,       # compiled JSON with abi + bytecode
        "deployer_key_index": 0,      # accounts[i] that deploys/owns the contract
    },
    "execution": {
        "rate": 500.0,
        "workers": 32,
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
    "monitoring": {"enabled": True, "interval": 1.0},
    "output": {"out_dir": "results/plain"},
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
            f"Copy config.plain.example.yml to {path} and edit it, or pass --config."
        )
    with open(path) as f:
        user = yaml.safe_load(f) or {}
    cfg = _deep_merge(DEFAULT_CONFIG, user)

    args = SimpleNamespace(
        experiment_name=cfg["experiment"]["name"],
        topology=cfg["network"]["topology"],
        accounts=cfg["network"]["accounts"],
        sweep=cfg["sweep"]["nproofs"],
        runs=int(cfg["sweep"]["runs"]),
        mode=cfg["sweep"]["mode"],
        num_vmnos=int(cfg["workload"]["num_vmnos"]),
        num_operators=int(cfg["workload"]["num_operators"]),
        batch_chunk=int(cfg["workload"]["batch_chunk"]),
        rate_per_second=int(cfg["workload"]["rate_per_second"]),
        rate_per_kb=int(cfg["workload"]["rate_per_kb"]),
        deployed_contracts=cfg["contracts"]["deployed_contracts"],
        plain_artifact=cfg["contracts"]["plain_artifact"],
        deployer_key_index=int(cfg["contracts"]["deployer_key_index"]),
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
        args.sweep = [int(x) for x in args.sweep.split(",") if x.strip()]
    else:
        args.sweep = [int(x) for x in args.sweep]

    if args.mode == "all":
        args.modes = list(MODES)
    elif args.mode in MODES:
        args.modes = [args.mode]
    else:
        raise ValueError(f"sweep.mode must be one of {MODES + ('all',)}, got {args.mode!r}")

    if args.latency_method not in ("block_time", "poll"):
        raise ValueError(
            f"execution.latency_method must be block_time or poll, got {args.latency_method!r}"
        )
    if args.num_operators < 2:
        raise ValueError("workload.num_operators must be >= 2")
    if not 1 <= args.batch_chunk <= 200:
        raise ValueError("workload.batch_chunk must be in 1..200 (contract MAX_BATCH_SIZE)")
    return args


# ============================================================
# Driver
# ============================================================

def main():
    cli = argparse.ArgumentParser(
        description="Plain (no-ZK, no-aggregation) roaming settlement baseline "
                    "for the Besu QBFT benchmark harness. Configuration lives "
                    "in the YAML file."
    )
    cli.add_argument("--config", default="config.plain.yml")
    args = load_config(cli.parse_args().config)

    os.makedirs(args.out_dir, exist_ok=True)
    detail_dir = os.path.join(args.out_dir, "detail")
    os.makedirs(detail_dir, exist_ok=True)

    resource_monitor = None
    if args.monitor_resources:
        resource_monitor = ResourceMonitor(args.monitor_interval)
        resource_monitor.start()
        resource_monitor.set_phase("setup")

    topology = load_json(args.topology)
    endpoints = build_rpc_endpoints(topology)
    web3s, chain_id = connect_web3(endpoints)

    accounts = load_accounts(args.accounts)
    if not accounts:
        raise RuntimeError("No workload accounts found.")
    assignment = assign_accounts(accounts, endpoints)

    # ---- contract + tariffs ----
    address = resolve_contract(args, web3s[0], chain_id, accounts)
    contract = web3s[0].eth.contract(address=address, abi=PLAIN_ABI)

    pairs = operator_pairs(args.num_operators)
    print(f"\nsetting up tariffs for {len(pairs)} operator pairs...")
    ensure_tariffs(web3s[0], contract, chain_id, accounts[args.deployer_key_index],
                   pairs, args.rate_per_second, args.rate_per_kb)

    if resource_monitor is not None:
        resource_monitor.set_phase("idle")

    salt = secrets.token_hex(4)
    print(f"\nsession-id salt for this process: {salt}")
    print(f"workload.num_vmnos={args.num_vmnos}  modes={args.modes}  "
          f"batch_chunk={args.batch_chunk}  latency={args.latency_method}")

    rows = []
    for nproofs in args.sweep:
        for run in range(1, args.runs + 1):
            print()
            print("=" * 60)
            print(f"nproofs/vmno={nproofs}  num_vmnos={args.num_vmnos}  run={run}")
            print("=" * 60)

            row = {
                "nproofs_per_vmno": nproofs,
                "num_vmnos": args.num_vmnos,
                "run": run,
                "batch_chunk": args.batch_chunk,
                "sessions_total": nproofs * args.num_vmnos,
                "contract": address,
            }
            base_ts = int(time.time()) - 86_400

            for mode in args.modes:
                calldata = build_leg_calldata(
                    contract, mode, salt, nproofs, run, args.num_vmnos,
                    args.num_operators, args.batch_chunk, base_ts,
                    args.rate_per_second, args.rate_per_kb,
                )
                label = f"plain_{mode}_n{nproofs}_vmnos{args.num_vmnos}_r{run}"
                txs, stats, gas_used, lat = run_leg(
                    label, mode, accounts, assignment, web3s, endpoints,
                    chain_id, contract, calldata, args, resource_monitor,
                )
                row.update(leg_metrics(mode, txs, stats, gas_used,
                                       row["sessions_total"], lat))
                write_leg_detail(detail_dir, mode, nproofs, args, run, txs, endpoints)

                print(f"  {mode}: tx={len(txs)} confirmed={stats['confirmed']} "
                      f"makespan={row[f'{mode}_makespan_s']}s "
                      f"gas/session={row[f'{mode}_gas_per_session']}")

                # Let the pool drain before the next leg / run starts.
                settle_network(f"{mode}_n{nproofs}_r{run}", accounts, assignment,
                               web3s, args.drain_timeout_s, resource_monitor)

            rows.append(row)

    if resource_monitor is not None:
        resource_monitor.stop()
        resource_monitor.write_csv(os.path.join(
            args.out_dir,
            f"{args.experiment_name}_{args.num_vmnos}_{args.rate}_{args.workers}_"
            f"{args.receipt_workers}_resource_usage.csv"))

    write_sweep_summary(os.path.join(
        args.out_dir,
        f"{args.experiment_name}_{args.num_vmnos}_{args.rate}_{args.workers}_"
        f"{args.receipt_workers}_sweep_summary.csv"), rows)
    print_highlights(rows, args.modes)


# ============================================================
# Output
# ============================================================

def write_leg_detail(detail_dir, mode, nproofs, args, run, txs, endpoints):
    path = os.path.join(
        detail_dir,
        f"plain_{mode}_n{nproofs}_{args.num_vmnos}_{args.rate}_{args.workers}_"
        f"{args.receipt_workers}_r{run}.csv",
    )
    fields = ["tx_index", "sender", "rpc_node", "tx_hash", "nonce", "submit_ts",
              "confirm_ts", "latency_s", "poll_latency_s", "block_number",
              "block_ts", "gas_used", "status", "error"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in txs:
            poll_lat = t.get("poll_latency_s")
            if poll_lat is None and t["submit_ts"] and t["confirm_ts"]:
                poll_lat = t["confirm_ts"] - t["submit_ts"]
            lat = t.get("latency_s")
            if lat is None:
                lat = poll_lat
            w.writerow({
                "tx_index": t["tx_index"], "sender": t["sender"],
                "rpc_node": endpoints[t["rpc_index"]]["name"],
                "tx_hash": t["tx_hash"], "nonce": t["nonce"],
                "submit_ts": t["submit_ts"], "confirm_ts": t["confirm_ts"],
                "latency_s": lat, "poll_latency_s": poll_lat,
                "block_number": t["block_number"],
                "block_ts": t.get("block_ts"),
                "gas_used": t.get("gas_used"), "status": t["status"],
                "error": t["error"],
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
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote: {path}")


def _med(rs, key):
    vals = [r[key] for r in rs if r.get(key) is not None]
    return statistics.median(vals) if vals else None


def _fmt(v, spec):
    return format(v, spec) if v is not None else format("n/a", spec.split(",")[0].split(".")[0])


def print_highlights(rows, modes):
    if not rows:
        return
    by_n = defaultdict(list)
    for r in rows:
        by_n[r["nproofs_per_vmno"]].append(r)

    print()
    print("=" * 100)
    print("PLAIN BASELINE HIGHLIGHTS (median across runs)")
    print("=" * 100)
    hdr = f"{'nproofs':>8} {'vmnos':>6}"
    for m in modes:
        hdr += f" | {m + ' tx':>12} {'gas/sess':>9} {'makespan':>9}"
    print(hdr)
    for n in sorted(by_n):
        rs = by_n[n]
        line = f"{n:>8} {rs[0]['num_vmnos']:>6}"
        for m in modes:
            tx = _med(rs, f"{m}_total_tx")
            gps = _med(rs, f"{m}_gas_per_session")
            ms = _med(rs, f"{m}_makespan_s")
            conf = _med(rs, f"{m}_confirmed")
            line += (f" | {_fmt(tx, '>12,.0f')} {_fmt(gps, '>9,.0f')} "
                     f"{_fmt(ms, '>8.1f')}s")
            if tx is not None and conf is not None and conf < tx:
                line += f" [{conf:.0f}/{tx:.0f} confirmed]"
        print(line)
    print("=" * 100)


if __name__ == "__main__":
    main()