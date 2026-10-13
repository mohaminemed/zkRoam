#!/usr/bin/env python3
"""
zkroam_threshold_ablation.py

Ablation over the attestation threshold t of SnarkPackAggregateAnchor.

For every t it
  1. authorizes t verifiers (accounts[0..t-1]) and calls setThreshold(t)   [owner txs]
  2. sends --sessions-per-point relayAggregateProof txs, each carrying EXACTLY t
     EIP-712 signatures (sorted by signer address, as the contract requires)
  3. records receipt gasUsed, calldata size, confirmation latency and checks that
     the settlement is finalized (isFinalized == True)
  4. (--check-boundary) sends one tx with t-1 signatures and checks it is NOT
     finalized, i.e. the threshold is really enforced

Outputs (in --out-dir):
  threshold_ablation_raw.csv      one row per relay tx
  threshold_ablation_summary.csv  one row per t (gas stats, marginal gas, ...)
  threshold_ablation_e2e.csv      per-session end-to-end gas = commit + relay/nproofs
  threshold_ablation.json         summary + linear fit  gas(t) = a + b*t

Run it on the same network as the main sweep, e.g.
  python3 workload/zkroam_threshold_ablation.py \\
      --topology networkFiles/topology.json --accounts accounts/accounts.json \\
      --deployed deployed_contracts.json --out-dir results/zkroam-wan-64vmnos \\
      --t-values 1 2 3 5 7 10 --nproofs 8 64 128 --individual-gas 230000

Notes
  * Verifier set grows monotonically (the contract has no removeVerifier), and the
    threshold is restored to its initial value at the end. The initial verifier
    must be accounts[0] and the contract owner must be --owner-index (default 0).
  * Signatures are produced off-chain and are free; only gas of the relay tx is
    measured. The relayer is a separate unprivileged account.
"""
import argparse
import csv
import json
import os
import secrets
import statistics
import time

from web3 import Web3

from besu_benchmark import build_rpc_endpoints, connect_web3, load_accounts, load_json
from zkroam_workload import (
    AGGREGATE_ANCHOR_ABI,
    CDR_COMMIT_ABI,
    anchor_domain_separator,
    attestation_digest,
    sign_attestation,
)

ADMIN_ABI = [
    {"type": "function", "name": "owner", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "threshold", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "verifierCount", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "isVerifier", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}], "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "isFinalized", "stateMutability": "view",
     "inputs": [{"name": "", "type": "bytes32"}], "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "addVerifier", "stateMutability": "nonpayable",
     "inputs": [{"name": "v", "type": "address"}], "outputs": []},
    {"type": "function", "name": "setThreshold", "stateMutability": "nonpayable",
     "inputs": [{"name": "t", "type": "uint256"}], "outputs": []},
]

ANALYTIC_COMMIT_GAS = 45_969  # see analytic breakdown; used only if no cdr_commit is deployed


class Chain:
    """Tiny sequential tx sender (one nonce counter per account)."""

    def __init__(self, w3, chain_id):
        self.w3 = w3
        self.chain_id = chain_id
        self.nonces = {}

    def _nonce(self, acct):
        a = acct["address"]
        if a not in self.nonces:
            self.nonces[a] = self.w3.eth.get_transaction_count(a, "pending")
        n = self.nonces[a]
        self.nonces[a] = n + 1
        return n

    def send(self, acct, to, data, gas=None, wait=True):
        w3 = self.w3
        if gas is None:
            gas = int(w3.eth.estimate_gas({"from": acct["address"], "to": to, "data": data}) * 1.25)
        tx = {
            "chainId": self.chain_id, "nonce": self._nonce(acct), "to": to,
            "data": data, "gas": gas, "gasPrice": w3.eth.gas_price, "value": 0,
        }
        signed = w3.eth.account.sign_transaction(tx, acct["private_key"])
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        t0 = time.time()
        h = w3.eth.send_raw_transaction(raw)
        if not wait:
            return h, None, None
        rc = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        return h, rc, time.time() - t0


def percentile(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topology", default="networkFiles/topology.json")
    ap.add_argument("--accounts", default="accounts/accounts.json")
    ap.add_argument("--deployed", default="deployed_contracts.json")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--t-values", nargs="+", type=int, default=[1, 2, 3, 5, 7, 10])
    ap.add_argument("--sessions-per-point", type=int, default=20,
                    help="relay txs per (t, run)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--nproofs", nargs="+", type=int, default=[8, 64, 128],
                    help="aggregation sizes used for the per-session e2e table")
    ap.add_argument("--attested-nproofs", type=int, default=64,
                    help="nproofs value placed inside the signed attestation "
                         "(does not influence gas)")
    ap.add_argument("--owner-index", type=int, default=0)
    ap.add_argument("--relayer-index", type=int, default=None,
                    help="account paying for relay txs (default: last account)")
    ap.add_argument("--commit-samples", type=int, default=10,
                    help="commit(sid,h_cdr) txs used to measure commit gas")
    ap.add_argument("--commit-gas", type=int, default=None,
                    help="override commit gas/session (default: measured, else analytic)")
    ap.add_argument("--individual-gas", type=float, default=None,
                    help="measured individual verifyProof gas/session, for the savings column")
    ap.add_argument("--no-check-boundary", action="store_true")
    args = ap.parse_args()

    t_values = sorted(set(args.t_values))
    if min(t_values) < 1 or max(t_values) > 50:
        raise SystemExit("t must be in [1, 50] (contract MAX_BATCH_SIZE = 50)")

    os.makedirs(args.out_dir, exist_ok=True)
    deployed = load_json(args.deployed)
    if not deployed.get("aggregate_anchor"):
        raise SystemExit(f"no 'aggregate_anchor' in {args.deployed}")
    anchor_addr = Web3.to_checksum_address(deployed["aggregate_anchor"]["address"])

    endpoints = build_rpc_endpoints(load_json(args.topology))
    web3s, chain_id = connect_web3(endpoints)
    w3 = web3s[0]
    chain = Chain(w3, chain_id)

    accounts = load_accounts(args.accounts)
    relayer_idx = args.relayer_index if args.relayer_index is not None else len(accounts) - 1
    if len(accounts) < max(t_values) + 2 or relayer_idx < max(t_values):
        raise SystemExit(f"need at least {max(t_values) + 2} accounts with the relayer "
                         f"outside accounts[0..{max(t_values) - 1}]")
    owner = accounts[args.owner_index]
    relayer = accounts[relayer_idx]

    anchor = w3.eth.contract(address=anchor_addr, abi=ADMIN_ABI + AGGREGATE_ANCHOR_ABI)
    if anchor.functions.owner().call().lower() != owner["address"].lower():
        raise SystemExit(f"accounts[{args.owner_index}] is not the anchor owner "
                         f"({anchor.functions.owner().call()}); use --owner-index")
    initial_threshold = anchor.functions.threshold().call()
    domain_sep = anchor_domain_separator(chain_id, anchor_addr)
    salt = secrets.token_hex(4)

    # ---------------- commit gas (per-session commit tx) ----------------
    commit_gas, commit_src = args.commit_gas, "override"
    if commit_gas is None:
        if deployed.get("cdr_commit"):
            caddr = Web3.to_checksum_address(deployed["cdr_commit"]["address"])
            cc = w3.eth.contract(abi=CDR_COMMIT_ABI)
            used = []
            for i in range(args.commit_samples):
                sid = Web3.keccak(text=f"zkroam-tabl-commit-{salt}-{i}")
                data = cc.encode_abi(abi_element_identifier="commit",
                                     args=[sid, Web3.keccak(secrets.token_bytes(32))])
                _, rc, _ = chain.send(relayer, caddr, data)
                if rc["status"] != 1:
                    raise SystemExit("commit tx reverted")
                used.append(rc["gasUsed"])
            commit_gas, commit_src = statistics.median(used), "measured"
        else:
            commit_gas, commit_src = ANALYTIC_COMMIT_GAS, "analytic"
    print(f"commit gas/session = {commit_gas:,.0f} ({commit_src})")

    relay_contract = w3.eth.contract(abi=AGGREGATE_ANCHOR_ABI)

    def build_relay(settlement_id, commitment, signer_accts):
        # contract requires strictly ascending signer addresses
        ordered = sorted(signer_accts, key=lambda a: int(a["address"], 16))
        digest = attestation_digest(domain_sep, settlement_id, commitment, args.attested_nproofs)
        signers = [Web3.to_checksum_address(a["address"]) for a in ordered]
        sigs = [sign_attestation(digest, a["private_key"]) for a in ordered]
        return relay_contract.encode_abi(
            abi_element_identifier="relayAggregateProof",
            args=[settlement_id, commitment, args.attested_nproofs, signers, sigs],
        )

    raw_rows, summary_rows = [], []
    try:
        for t in t_values:
            # ---- configure verifier set + threshold (owner txs) ----
            for a in accounts[:t]:
                if not anchor.functions.isVerifier(a["address"]).call():
                    data = anchor.encode_abi("addVerifier", args=[Web3.to_checksum_address(a["address"])])
                    _, rc, _ = chain.send(owner, anchor_addr, data)
                    if rc["status"] != 1:
                        raise SystemExit(f"addVerifier({a['address']}) reverted")
            data = anchor.encode_abi("setThreshold", args=[t])
            _, rc, _ = chain.send(owner, anchor_addr, data)
            if rc["status"] != 1:
                raise SystemExit(f"setThreshold({t}) reverted")
            assert anchor.functions.threshold().call() == t
            vcount = anchor.functions.verifierCount().call()
            signer_set = accounts[:t]

            # ---- one gas estimate per t (fresh settlement id, same shape) ----
            probe = build_relay(Web3.keccak(text=f"zkroam-tabl-probe-{salt}-t{t}"),
                                Web3.keccak(b"probe"), signer_set)
            gas_limit = int(w3.eth.estimate_gas(
                {"from": relayer["address"], "to": anchor_addr, "data": probe}) * 1.3)

            gas_vals, lat_vals, finalized_ok, cd_bytes = [], [], 0, 0
            n_tx = 0
            for run in range(args.runs):
                for i in range(args.sessions_per_point):
                    sid = Web3.keccak(text=f"zkroam-tabl-{salt}-t{t}-r{run}-{i}")
                    commitment = Web3.keccak(secrets.token_bytes(64))
                    data = build_relay(sid, commitment, signer_set)
                    cd_bytes = len(Web3.to_bytes(hexstr=data))
                    _, rc, lat = chain.send(relayer, anchor_addr, data, gas=gas_limit)
                    n_tx += 1
                    ok = rc["status"] == 1
                    fin = bool(anchor.functions.isFinalized(sid).call()) if ok else False
                    finalized_ok += int(ok and fin)
                    if ok:
                        gas_vals.append(rc["gasUsed"])
                        lat_vals.append(lat)
                    raw_rows.append({
                        "t": t, "run": run, "i": i, "settlement_id": sid.hex(),
                        "status": rc["status"], "gas_used": rc["gasUsed"],
                        "gas_limit": gas_limit, "calldata_bytes": cd_bytes,
                        "confirm_latency_s": round(lat, 4), "finalized": fin,
                    })

            boundary = ""
            if t > 1 and not args.no_check_boundary:
                sid = Web3.keccak(text=f"zkroam-tabl-{salt}-t{t}-boundary")
                data = build_relay(sid, Web3.keccak(b"boundary"), signer_set[: t - 1])
                _, rc, _ = chain.send(relayer, anchor_addr, data, gas=gas_limit)
                not_fin = rc["status"] == 1 and not anchor.functions.isFinalized(sid).call()
                boundary = "ok" if not_fin else "VIOLATION"
            elif t == 1:
                boundary = "n/a"

            med = statistics.median(gas_vals) if gas_vals else None
            summary_rows.append({
                "t": t, "verifier_count": vcount, "txs": n_tx,
                "success_rate_pct": round(100.0 * len(gas_vals) / n_tx, 2),
                "finalized_rate_pct": round(100.0 * finalized_ok / n_tx, 2),
                "relay_gas_median": med,
                "relay_gas_mean": statistics.mean(gas_vals) if gas_vals else None,
                "relay_gas_min": min(gas_vals) if gas_vals else None,
                "relay_gas_max": max(gas_vals) if gas_vals else None,
                "calldata_bytes": cd_bytes,
                "confirm_latency_p50_s": percentile(lat_vals, 50),
                "confirm_latency_p99_s": percentile(lat_vals, 99),
                "boundary_check_t_minus_1_not_final": boundary,
            })
            print(f"t={t:<3d} relay gas median={med:,.0f}  calldata={cd_bytes}B  "
                  f"finalized={finalized_ok}/{n_tx}  boundary={boundary}")
    finally:
        # restore the original threshold so the main sweep (1 signer) still finalizes
        try:
            data = anchor.encode_abi("setThreshold", args=[initial_threshold])
            chain.send(owner, anchor_addr, data)
            print(f"threshold restored to {initial_threshold}")
        except Exception as e:
            print(f"WARNING: could not restore threshold {initial_threshold}: {e}")

    # ---------------- marginal gas + linear fit ----------------
    prev = None
    for r in summary_rows:
        g = r["relay_gas_median"]
        r["marginal_gas_vs_prev_t"] = (
            None if prev is None or g is None else
            (g - prev["relay_gas_median"]) / (r["t"] - prev["t"]))
        r["gas_per_attestation"] = None if g is None else g / r["t"]
        prev = r
    fit = None
    pts = [(r["t"], r["relay_gas_median"]) for r in summary_rows if r["relay_gas_median"]]
    if len(pts) >= 2:
        slope, intercept = statistics.linear_regression([p[0] for p in pts], [p[1] for p in pts])
        fit = {"intercept_gas": intercept, "slope_gas_per_extra_attestation": slope}

    # ---------------- end-to-end per-session table ----------------
    e2e_rows = []
    for r in summary_rows:
        A = r["relay_gas_median"]
        if A is None:
            continue
        for n in args.nproofs:
            agg = commit_gas + A / n
            row = {"t": r["t"], "nproofs": n, "commit_gas": commit_gas,
                   "commit_gas_source": commit_src, "relay_gas_per_aggregate": A,
                   "relay_gas_per_session": A / n,
                   "e2e_aggregate_gas_per_session": agg,
                   "commit_share_pct": 100.0 * commit_gas / agg}
            if args.individual_gas:
                ind = commit_gas + args.individual_gas
                row["e2e_individual_gas_per_session"] = ind
                row["e2e_savings_pct"] = 100.0 * (1 - agg / ind)
            e2e_rows.append(row)

    def dump(name, rows):
        if not rows:
            return
        keys = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with open(os.path.join(args.out_dir, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)

    dump("threshold_ablation_raw.csv", raw_rows)
    dump("threshold_ablation_summary.csv", summary_rows)
    dump("threshold_ablation_e2e.csv", e2e_rows)
    with open(os.path.join(args.out_dir, "threshold_ablation.json"), "w") as f:
        json.dump({"anchor": anchor_addr, "commit_gas": commit_gas,
                   "commit_gas_source": commit_src, "linear_fit": fit,
                   "summary": summary_rows, "e2e": e2e_rows}, f, indent=2, default=str)

    # ---------------- console tables ----------------
    print("\n" + "=" * 78)
    print(f"{'t':>3} {'relay_gas':>11} {'gas/attest':>11} {'marginal':>10} "
          f"{'calldata_B':>10} {'final%':>7} {'boundary':>9}")
    for r in summary_rows:
        mg = r["marginal_gas_vs_prev_t"]
        print(f"{r['t']:>3} {r['relay_gas_median']:>11,.0f} {r['gas_per_attestation']:>11,.0f} "
              f"{(f'{mg:,.0f}' if mg is not None else '-'):>10} {r['calldata_bytes']:>10} "
              f"{r['finalized_rate_pct']:>7.1f} {r['boundary_check_t_minus_1_not_final']:>9}")
    if fit:
        print(f"\nfit: relay_gas(t) ~= {fit['intercept_gas']:,.0f} + "
              f"{fit['slope_gas_per_extra_attestation']:,.0f} * t")
    print(f"\nper-session e2e (commit {commit_gas:,.0f} [{commit_src}] + relay/nproofs):")
    hdr = f"{'t':>3} {'nproofs':>8} {'e2e_agg/sess':>13} {'commit_%':>9}"
    if args.individual_gas:
        hdr += f" {'e2e_ind/sess':>13} {'savings_%':>10}"
    print(hdr)
    for r in e2e_rows:
        line = (f"{r['t']:>3} {r['nproofs']:>8} {r['e2e_aggregate_gas_per_session']:>13,.0f} "
                f"{r['commit_share_pct']:>9.1f}")
        if args.individual_gas:
            line += f" {r['e2e_individual_gas_per_session']:>13,.0f} {r['e2e_savings_pct']:>10.1f}"
        print(line)
    print(f"\nWrote ablation CSV/JSON to {args.out_dir}")


if __name__ == "__main__":
    main()
