#!/usr/bin/env python3
"""
agreement_common_e2e.py

Shared plumbing for zkroam_workload_e2e.py and plain_workload_e2e.py, which now
benchmark the REAL settlement contracts (AgreementIndividual,
AgreementAggregate, AgreementPlain) instead of standalone verifier/commit
stand-ins. Everything the two workloads must do identically lives here so the
three paths are measured the same way:

  * a FRESH agreement instance per leg (no state leaks between legs/runs),
  * identical session setup (HMNO-signed startRoamingSession, escrow, same
    rates, same per-session CDR / estimate model),
  * identical commit leg (UE -> submitCDR) for the paths that have one,
  * identical per-leg metrics (gas from receipts, block-time latency, ...).

Setup (deploy, mint, approve, startRoamingSession, UE funding) is NOT measured;
only the commit and settlement legs are.

Per leg the workloads call, in order:
    deploy_agreement()   -> fresh contract + HMNO approvals
    start_sessions()     -> N = nproofs*num_vmnos PENDING sessions
    commit_leg()         -> N submitCDR txs, one per session (measured)
    <settlement leg(s)>  -> workload specific (measured)
"""

import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_keys import keys as eth_keys
from web3 import Web3

from besu_benchmark import (
    submit_all,
    poll_all_receipts,
    compute_statistics,
    load_json,
)

SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SESSION_STARTED_SIG = "SessionStarted(uint256,address,address,address,uint256)"


# ============================================================
# Artifacts (Foundry layout: out/<File>.sol/<Contract>.json)
# ============================================================

def forge_artifact(forge_out, source_file, contract):
    return os.path.join(forge_out, source_file, f"{contract}.json")


def load_artifact(path):
    """-> (abi, bytecode_hex). Accepts Foundry, Hardhat and plain {abi,bytecode}."""
    art = load_json(path)
    abi = art["abi"]
    bc = art.get("bytecode", art.get("bin"))
    if isinstance(bc, dict):                      # Foundry: {"object": "0x..."}
        bc = bc["object"]
    bc = str(bc)
    return abi, bc if bc.startswith("0x") else "0x" + bc


# ============================================================
# Chain helpers (raw RPC: web3.py's get_block() trips on QBFT extraData)
# ============================================================

def _as_int(block, key, alt):
    v = block.get(key, block.get(alt))
    return int(v, 16) if isinstance(v, str) else int(v)


def latest_block_gas_limit(w3):
    res = w3.provider.make_request("eth_getBlockByNumber", ["latest", False])
    if res.get("error"):
        raise RuntimeError(f"eth_getBlockByNumber failed: {res['error']}")
    return _as_int(res["result"], "gasLimit", "gas_limit")


def block_timestamp(w3, block_number):
    res = w3.provider.make_request("eth_getBlockByNumber", [hex(block_number), False])
    if res.get("error") or not res.get("result"):
        return None
    return _as_int(res["result"], "timestamp", "timestamp")


def check_gas_fits_block(label, gas, w3, hint):
    limit = latest_block_gas_limit(w3)
    if gas > 0.9 * limit:
        raise RuntimeError(
            f"{label}: estimated {gas} gas/tx exceeds 90% of the block gas limit "
            f"({limit}). {hint}")
    return limit


def apply_block_time_latency(transactions, web3s, method="block_time"):
    """Per-tx latency = (mining block timestamp) - (tx's OWN submit_ts), clamped to
    [0, poll latency]. Sets latency_s / poll_latency_s / block_ts on every tx and
    returns {"p50","p99","mean","n"} over confirmed txs."""
    cache = {}
    for t in transactions:
        t["latency_s"] = t["poll_latency_s"] = t["block_ts"] = None
        if t["submit_ts"] is None or t["confirm_ts"] is None:
            continue
        poll_lat = t["confirm_ts"] - t["submit_ts"]
        t["poll_latency_s"] = poll_lat
        lat = poll_lat
        if (method == "block_time" and t["status"] == "confirmed"
                and t["block_number"] is not None):
            bn = t["block_number"]
            if bn not in cache:
                try:
                    cache[bn] = block_timestamp(web3s[t["rpc_index"]], bn)
                except Exception:  # noqa: BLE001
                    cache[bn] = None
            if cache[bn] is not None:
                t["block_ts"] = cache[bn]
                lat = min(poll_lat, max(0.0, cache[bn] - t["submit_ts"]))
        t["latency_s"] = lat

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


def fetch_gas_used(txs, web3s, workers=16):
    """Real gasUsed from receipts, for confirmed txs."""
    def one(t):
        try:
            t["gas_used"] = web3s[t["rpc_index"]].eth.get_transaction_receipt(
                t["tx_hash"])["gasUsed"]
        except Exception:  # noqa: BLE001
            t["gas_used"] = None
    todo = [t for t in txs if t["status"] == "confirmed" and t["tx_hash"]]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, todo))
    return [t["gas_used"] for t in todo if t["gas_used"] is not None]


# ============================================================
# Environment: connection + nonce bookkeeping + leg runner
# ============================================================

class Env:
    """Bundles what every setup/measurement step needs.

    Nonces are tracked locally per ADDRESS (pool accounts and generated UE
    accounts alike) and re-synced from the chain whenever a tx of that sender
    did not end up confirmed/reverted, so a failed leg cannot leave a gap."""

    def __init__(self, web3s, chain_id, accounts, assignment, endpoints, cfg):
        self.web3s = web3s
        self.chain_id = chain_id
        self.accounts = accounts
        self.assignment = assignment
        self.endpoints = endpoints
        self.cfg = cfg                       # SimpleNamespace of execution knobs
        self._nonce = {}
        self.monitor = None

    # ---- routing -------------------------------------------------------
    def rpc_of(self, acct):
        if "_rpc" in acct:                   # generated accounts
            return acct["_rpc"]
        return self.assignment[acct["index"]]

    # ---- nonces --------------------------------------------------------
    def _load_nonce(self, acct):
        w3 = self.web3s[self.rpc_of(acct)]
        return w3.eth.get_transaction_count(Web3.to_checksum_address(acct["address"]), "pending")

    def preload_nonces(self, accts, workers=16):
        todo = [a for a in accts if a["address"] not in self._nonce]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for a, n in zip(todo, ex.map(self._load_nonce, todo)):
                self._nonce[a["address"]] = n

    def take_nonce(self, acct):
        if acct["address"] not in self._nonce:
            self._nonce[acct["address"]] = self._load_nonce(acct)
        n = self._nonce[acct["address"]]
        self._nonce[acct["address"]] = n + 1
        return n

    def forget_nonces(self, txs):
        for t in txs:
            if t["status"] not in ("confirmed", "reverted"):
                self._nonce.pop(t["sender"], None)

    # ---- phases --------------------------------------------------------
    def phase(self, name):
        if self.monitor is not None:
            self.monitor.set_phase(name)

    def drain(self, label, senders, timeout_s):
        """Wait until none of `senders` has pending-but-unmined txs."""
        def backlog(acct):
            w3 = self.web3s[self.rpc_of(acct)]
            a = Web3.to_checksum_address(acct["address"])
            try:
                return (w3.eth.get_transaction_count(a, "pending")
                        - w3.eth.get_transaction_count(a, "latest"))
            except Exception:  # noqa: BLE001
                return 0
        start = time.time()
        uniq = {a["address"]: a for a in senders}.values()
        while True:
            with ThreadPoolExecutor(max_workers=16) as ex:
                remaining = sum(ex.map(backlog, uniq))
            if remaining == 0:
                print(f"  [settle:{label}] pool drained after {time.time() - start:.1f}s")
                return True
            if time.time() - start > timeout_s:
                print(f"  [settle:{label}] WARNING: {remaining} txs still pending after "
                      f"{time.time() - start:.0f}s")
                return False
            time.sleep(2.0)


def run_leg(env, label, specs, measured=True):
    """Sign, submit, confirm `specs` and return a LegResult.

    spec = {"sender": acct, "to": addr, "data": bytes|hex, "gas": int}
    Order of `specs` is preserved per sender (nonces are assigned in order)."""
    cfg = env.cfg
    env.preload_nonces([s["sender"] for s in specs])
    gas_price = env.web3s[0].eth.gas_price
    txs = []
    for i, sp in enumerate(specs):
        acct = sp["sender"]
        rpc = env.rpc_of(acct)
        data = sp["data"] if isinstance(sp["data"], str) else Web3.to_hex(sp["data"])
        signed = env.web3s[rpc].eth.account.sign_transaction({
            "chainId": env.chain_id, "nonce": env.take_nonce(acct),
            "to": sp["to"], "value": 0, "gas": sp["gas"],
            "gasPrice": gas_price, "data": data,
        }, acct["private_key"])
        txs.append({
            "tx_index": i, "account_index": acct.get("index", -1),
            "sender": acct["address"], "rpc_index": rpc,
            "raw": signed.raw_transaction,
            "nonce": env._nonce[acct["address"]] - 1,
            "tx_hash": None, "submit_ts": None, "confirm_ts": None,
            "block_number": None, "status": "prepared", "error": None,
            "gas_used": None,
        })

    timeout = max(cfg.receipt_timeout, len(txs) * cfg.receipt_timeout_per_tx)
    print(f"\n>>> {'leg' if measured else 'setup'}: {label} ({len(txs)} tx, "
          f"gas/tx={specs[0]['gas'] if specs else 0})", flush=True)
    env.phase(f"{label}:submission")
    submit_all(txs, env.web3s, target_rate=cfg.rate, workers=cfg.workers)
    env.phase(f"{label}:confirmation")
    poll_all_receipts(txs, env.web3s, workers=cfg.receipt_workers,
                      timeout=timeout, poll_interval=cfg.poll_interval)
    env.phase("idle")

    stats = compute_statistics(txs)
    gas_used = fetch_gas_used(txs, env.web3s)
    lat = apply_block_time_latency(txs, env.web3s, cfg.latency_method)
    env.forget_nonces(txs)

    if stats["reverted"] or stats["timeout"] or stats["failed_submission"]:
        print(f"  WARNING {label}: reverted={stats['reverted']} timeout={stats['timeout']} "
              f"failed_submission={stats['failed_submission']}", flush=True)
        errs = {}
        for t in txs:
            if t["status"] == "failed":
                key = str(t["error"])[:200]
                errs.setdefault(key, []).append(t["sender"])
        for msg, who in list(errs.items())[:5]:
            print(f"    submit error x{len(who)} ({len(set(who))} sender(s)): {msg}", flush=True)
        if not measured:
            raise RuntimeError(f"setup step '{label}' had failing txs; aborting "
                               f"(a measurement on top of it would be meaningless)")
    return SimpleNamespace(
        label=label, txs=txs, stats=stats, gas_used=gas_used, lat=lat,
        gas_limit=specs[0]["gas"] if specs else None,
        senders=[s["sender"] for s in specs])


def _setup_batches(specs, chunk, per_sender):
    """Split `specs` (order preserved) into batches of at most `chunk` txs AND at most
    `per_sender` txs from any one sender. Besu caps how much of its txpool a single
    account may occupy (--tx-pool-limit-by-account-percentage); setup legs send all
    their txs from only 2 HMNO accounts, so a big batch gets rejected at submission."""
    batches, cur, cnt = [], [], {}
    for sp in specs:
        a = sp["sender"]["address"]
        if cur and (len(cur) >= chunk or (per_sender and cnt.get(a, 0) >= per_sender)):
            batches.append(cur)
            cur, cnt = [], {}
        cur.append(sp)
        cnt[a] = cnt.get(a, 0) + 1
    if cur:
        batches.append(cur)
    return batches


def run_setup_chunked(env, label, specs, chunk):
    """Setup legs in confirmed batches so the txpool is never flooded (neither in total
    nor per sender: cfg.setup_per_sender, 0 = unlimited)."""
    per_sender = int(getattr(env.cfg, "setup_per_sender", 0) or 0)
    batches = _setup_batches(specs, chunk, per_sender)
    k = 0
    for b in batches:
        res = run_leg(env, f"{label}[{k}:{k + len(b)}]", b, measured=False)
        env.drain(label, res.senders, env.cfg.drain_timeout_s)
        k += len(b)


def settle_leg(env, res):
    """After a MEASURED leg: wait for the pool to drain."""
    env.drain(res.label, res.senders, env.cfg.drain_timeout_s)


def estimate_gas(env, sender_addr, to, data, margin=1.15, what="call", cap=None):
    """estimate_gas must succeed on a valid call; failure means it would revert.
    `cap` bounds the gas the estimate may use, so low-balance senders (UEs) are
    not rejected for being unable to afford the whole block gas limit."""
    call = {"from": Web3.to_checksum_address(sender_addr), "to": to,
            "data": data if isinstance(data, str) else Web3.to_hex(data)}
    if cap:
        call["gas"] = cap
    try:
        g = env.web3s[0].eth.estimate_gas(call)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"estimate_gas failed for {what} - it would revert: {e}") from e
    return int(g * margin)


# ============================================================
# Metrics (same columns for every leg of every path)
# ============================================================

def leg_columns(prefix, res):
    s = res.stats
    gas_vals = res.gas_used
    return {
        f"{prefix}_total_tx": len(res.txs),
        f"{prefix}_gas_limit_per_tx": res.gas_limit,
        f"{prefix}_gas_used_per_tx": statistics.median(gas_vals) if gas_vals else None,
        f"{prefix}_total_gas_used": sum(gas_vals) if gas_vals else None,
        f"{prefix}_confirmed": s["confirmed"],
        f"{prefix}_reverted": s["reverted"],
        f"{prefix}_success_rate_pct": s["success_rate_pct"],
        f"{prefix}_submit_throughput_tx_s": s["submission_throughput_tx_s"],
        f"{prefix}_confirm_throughput_tx_s": s["confirmation_throughput_tx_s"],
        f"{prefix}_latency_p50_s": res.lat["p50"],
        f"{prefix}_latency_p99_s": res.lat["p99"],
        f"{prefix}_confirm_duration_s": s["confirm_duration_s"],
        # a leg is only comparable if every tx confirmed AND succeeded
        f"{prefix}_valid": (s["confirmed"] == len(res.txs)
                            and s["reverted"] == 0 and len(gas_vals) == len(res.txs)),
    }


# ============================================================
# Deployment
# ============================================================

def _send_and_wait(env, acct, tx, timeout=180):
    w3 = env.web3s[env.rpc_of(acct)]
    tx = dict(tx)
    tx.setdefault("chainId", env.chain_id)
    tx["nonce"] = env.take_nonce(acct)
    tx.setdefault("gasPrice", w3.eth.gas_price)
    tx.setdefault("value", 0)
    signed = w3.eth.account.sign_transaction(tx, acct["private_key"])
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
    if rc["status"] != 1:
        env._nonce.pop(acct["address"], None)
        raise RuntimeError(f"setup tx reverted: {Web3.to_hex(h)}")
    return rc


def deploy_contract(env, deployer, abi, bytecode, *ctor_args):
    """-> (checksum address, deploy block number)."""
    w3 = env.web3s[env.rpc_of(deployer)]
    data = w3.eth.contract(abi=abi, bytecode=bytecode).constructor(*ctor_args).data_in_transaction
    gas = int(w3.eth.estimate_gas({"from": Web3.to_checksum_address(deployer["address"]),
                                   "data": data}) * 1.2)
    rc = _send_and_wait(env, deployer, {"to": None, "gas": gas, "data": data})
    return Web3.to_checksum_address(rc["contractAddress"]), rc["blockNumber"]


def send_call(env, acct, contract, fn, args, gas=None):
    data = contract.encode_abi(abi_element_identifier=fn, args=list(args))
    if gas is None:
        gas = estimate_gas(env, acct["address"], contract.address, data, 1.3, fn)
    return _send_and_wait(env, acct, {"to": contract.address, "gas": gas, "data": data})


class Token:
    """BenchToken, deployed once per workload process."""

    def __init__(self, env, deployer, forge_out):
        self.abi, bc = load_artifact(forge_artifact(forge_out, "BenchToken.sol", "BenchToken"))
        self.address, _ = deploy_contract(env, deployer, self.abi, bc)
        self.contract = env.web3s[0].eth.contract(address=self.address, abi=self.abi)
        print(f"bench token: {self.address}")

    def mint(self, env, acct, to, amount):
        send_call(env, acct, self.contract, "mint", [to, amount])

    def approve(self, env, owner, spender, amount=2 ** 255):
        send_call(env, owner, self.contract, "approve", [spender, amount])


def deploy_agreement(env, kind, deployer, mnos, rates, token, forge_out, extra=None):
    """Deploy a FRESH agreement of `kind` ('individual'|'aggregate'|'plain') and
    approve it from both HMNOs. `extra` is the last ctor arg for individual
    (verifier address) and aggregate (anchor address).
    Returns SimpleNamespace(address, contract, abi, deploy_block)."""
    src, name = {"individual": ("AgreementIndividual.sol", "AgreementIndividual"),
                 "aggregate": ("AgreementAggregate.sol", "AgreementAggregate"),
                 "plain": ("AgreementPlain.sol", "AgreementPlain")}[kind]
    abi, bc = load_artifact(forge_artifact(forge_out, src, name))
    expiry = int(time.time()) + 10 * 365 * 86400
    args = [mnos[0]["address"], mnos[1]["address"],
            (rates["sms"], rates["voice"], rates["data"]), expiry, token.address]
    if kind != "plain":
        args.append(extra)
    addr, block = deploy_contract(env, deployer, abi, bc,
                                  *[Web3.to_checksum_address(a) if isinstance(a, str) and a.startswith("0x") else a
                                    for a in args])
    for m in mnos:
        token.approve(env, m, addr)
    contract = env.web3s[0].eth.contract(address=addr, abi=abi)
    print(f"agreement[{kind}]: {addr}")
    return SimpleNamespace(address=addr, contract=contract, abi=abi,
                           deploy_block=block, kind=kind)


# ============================================================
# Accounts for UEs (generated, deterministic)
# ============================================================

def make_ue_accounts(n, seed, n_rpc):
    out = []
    for i in range(n):
        k = int.from_bytes(Web3.keccak(text=f"{seed}-ue-{i}"), "big") % (SECP256K1_N - 1) + 1
        pk = eth_keys.PrivateKey(k.to_bytes(32, "big"))
        out.append({"index": 1_000_000 + i, "address": pk.public_key.to_checksum_address(),
                    "private_key": "0x" + pk.to_bytes().hex(), "_rpc": i % n_rpc})
    return out


def fund_ue_accounts(env, funder, ue_accts, tx_budget, chunk, fund_wei=None):
    """Give each UE enough native balance for `tx_budget` submitCDR txs.
    Skipped on zero-gas-price chains."""
    gas_price = env.web3s[0].eth.gas_price
    if gas_price == 0:
        print("gas price is 0: UE accounts need no funding")
        return
    want = fund_wei or gas_price * 150_000 * tx_budget

    def bal(a):
        return env.web3s[a["_rpc"]].eth.get_balance(a["address"])
    with ThreadPoolExecutor(max_workers=16) as ex:
        balances = list(ex.map(bal, ue_accts))
    todo = [a for a, b in zip(ue_accts, balances) if b < want]
    if not todo:
        return
    print(f"funding {len(todo)} UE accounts with {want} wei each from {funder['address']}")
    _fund_chunked(env, funder, todo, want, chunk)


def _fund_chunked(env, funder, todo, want, chunk):
    gas_price = env.web3s[0].eth.gas_price
    w3 = env.web3s[env.rpc_of(funder)]
    for k in range(0, len(todo), chunk):
        txs = []
        for a in todo[k:k + chunk]:
            signed = w3.eth.account.sign_transaction({
                "chainId": env.chain_id, "nonce": env.take_nonce(funder),
                "to": a["address"], "value": want, "gas": 21_000, "gasPrice": gas_price,
            }, funder["private_key"])
            txs.append({"tx_index": len(txs), "account_index": funder.get("index", -1),
                        "sender": funder["address"], "rpc_index": env.rpc_of(funder),
                        "raw": signed.raw_transaction, "nonce": env._nonce[funder["address"]] - 1,
                        "tx_hash": None, "submit_ts": None, "confirm_ts": None,
                        "block_number": None, "status": "prepared", "error": None,
                        "gas_used": None})
        submit_all(txs, env.web3s, target_rate=env.cfg.rate, workers=env.cfg.workers)
        poll_all_receipts(txs, env.web3s, workers=env.cfg.receipt_workers,
                          timeout=max(env.cfg.receipt_timeout, len(txs) * env.cfg.receipt_timeout_per_tx),
                          poll_interval=env.cfg.poll_interval)
        env.forget_nonces(txs)
        bad = [t for t in txs if t["status"] != "confirmed"]
        if bad:
            raise RuntimeError(f"UE funding failed for {len(bad)} accounts")
        env.drain("fund_ue", [funder], env.cfg.drain_timeout_s)


# ============================================================
# Session model (identical for every path)
# ============================================================

def session_plan(n_groups, nproofs, rates, margin_pct, min_estimate=1):
    """One entry per session, group-major: session u belongs to group u // nproofs
    (a "VMNO"). CDR = (sms, voiceSeconds, dataKB); cost = sms*r.sms + voice*r.voice +
    data*r.data; estimate = cost * (1 + margin) so every settlement refunds a bit."""
    plan = []
    for g in range(n_groups):
        for i in range(nproofs):
            sms = 1 + (i * 3 + g) % 20
            voice = 30 + (i * 37 + g * 11) % 3570
            data = 1000 + (i * 7919 + g * 104729) % 500_000
            cost = sms * rates["sms"] + voice * rates["voice"] + data * rates["data"]
            est = max(min_estimate, cost + cost * margin_pct // 100 + 1)
            plan.append({"group": g, "idx": i, "cdr": (sms, voice, data),
                         "cost": cost, "est": est})
    return plan


def plain_cdr_hash(cdr):
    """keccak256(abi.encode(CDR{uint64,uint64,uint64})) - matches AgreementPlain."""
    return Web3.keccak(abi_encode(["uint64", "uint64", "uint64"], list(cdr)))


BN254_R = 21888242871839275222246405745257275088548364400416034343698204186575808495617


def field_cdr_hash(cdr):
    """keccak256(abi.encode(cdr)) reduced mod the BN254 scalar field. The ZK paths
    need this: Groth16Verifier.checkField() rejects public inputs >= r BEFORE the
    pairing, so a raw 256-bit hash would exit early and understate verifier gas."""
    h = int.from_bytes(plain_cdr_hash(cdr), "big") % BN254_R
    return h.to_bytes(32, "big")


def sign_start(agreement_addr, hmno, ue_addr, est, nonce):
    """HMNO signature over keccak(abi.encode(agreement, ue, est, nonce)), raw hash."""
    h = Web3.keccak(abi_encode(["address", "address", "uint256", "uint256"],
                               [agreement_addr, ue_addr, est, nonce]))
    pk = eth_keys.PrivateKey(bytes.fromhex(hmno["private_key"].replace("0x", "")))
    sig = pk.sign_msg_hash(h)
    return sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v + 27])


def start_sessions(env, ag, mnos, ue_accts, plan, setup_chunk):
    """Start len(plan) sessions on `ag`. Session u is started by HMNO mnos[u % 2]
    (so both payout directions are exercised), signed by that HMNO.

    Returns [(session_id, hmno_address)] indexed by plan position. Session ids are
    read back from SessionStarted logs because the two HMNO lanes can interleave."""
    specs = []
    lane_nonce = [0, 0]                      # fresh agreement: contract nonces start at 0
    for u, p in enumerate(plan):
        lane = u % 2
        hm = mnos[lane]
        ue = ue_accts[u]["address"]
        sig = sign_start(ag.address, hm, ue, p["est"], lane_nonce[lane])
        data = ag.contract.encode_abi(
            abi_element_identifier="startRoamingSession",
            args=[ue, p["est"], lane_nonce[lane], sig])
        lane_nonce[lane] += 1
        specs.append({"sender": hm, "to": ag.address, "data": data, "gas": 0})
    gas = estimate_gas(env, mnos[0]["address"], ag.address, specs[0]["data"], 1.4,
                       "startRoamingSession")
    for s in specs:
        s["gas"] = gas
    run_setup_chunked(env, f"start_sessions[{ag.kind}]", specs, setup_chunk)
    return read_started_sessions(env, ag, ue_accts)


def read_started_sessions(env, ag, ue_accts, span=1500):
    w3 = env.web3s[0]
    topic0 = Web3.to_hex(Web3.keccak(text=SESSION_STARTED_SIG))
    latest = w3.eth.block_number
    by_ue = {}
    for lo in range(ag.deploy_block, latest + 1, span):
        logs = w3.eth.get_logs({"address": ag.address, "fromBlock": lo,
                                "toBlock": min(lo + span - 1, latest), "topics": [topic0]})
        for lg in logs:
            sid = int.from_bytes(bytes(lg["topics"][1]), "big")
            hmno = Web3.to_checksum_address(bytes(lg["topics"][2])[-20:])
            ue, _est = abi_decode(["address", "uint256"], bytes(lg["data"]))
            by_ue[Web3.to_checksum_address(ue)] = (sid, hmno)
    out = []
    missing = 0
    for a in ue_accts:
        v = by_ue.get(Web3.to_checksum_address(a["address"]))
        if v is None:
            missing += 1
        out.append(v)
    if missing:
        raise RuntimeError(f"{missing} of {len(ue_accts)} sessions were not started")
    return out


def commit_leg(env, ag, ue_accts, sessions, cdr_hashes, label):
    """One submitCDR(sessionId, hash) per session, sent BY the session's UE
    (msg.sender must be the UE). Measured."""
    specs = [{"sender": ue_accts[u], "to": ag.address,
              "data": ag.contract.encode_abi(abi_element_identifier="submitCDR",
                                             args=[sessions[u][0], cdr_hashes[u]]),
              "gas": 0} for u in range(len(sessions))]
    gas = estimate_gas(env, ue_accts[0]["address"], ag.address, specs[0]["data"], 1.3, "submitCDR",
                       cap=1_000_000)
    for s in specs:
        s["gas"] = gas
    res = run_leg(env, label, specs)
    settle_leg(env, res)
    return res
