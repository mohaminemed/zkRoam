#!/usr/bin/env python3
"""
plain_workload_e2e.py

PLAIN baseline for the zkRoam benchmark, run against the real settlement
contract AgreementPlain.sol. It has the SAME escrow, session state, per-session
cost bound, events and payouts as AgreementIndividual / AgreementAggregate; the
only difference is how the cost is validated: the full CDR goes on-chain in
clear, the contract checks it against the hash the UE committed and recomputes
the cost from the agreement's rates. No proof, no aggregation.

  sweep.nproofs      -> sessions ONE VMNO settles together
  workload.num_vmnos -> how many VMNOs (groups of sessions) are settled

Legs (sweep.mode: individual | batch | all). Each mode gets a FRESH
AgreementPlain and its own set of N = nproofs * num_vmnos sessions:

  commit      N txs, UE -> submitCDR(sessionId, keccak(cdr))            (measured)
  individual  N txs, terminateRoamingSession(sessionId, cdr)            (measured)
  batch       num_vmnos * ceil(nproofs / batch_chunk) txs,
              settleBatch(sessionIds[], cdrs[])                          (measured)
              the plain alternative to aggregation: one tx per VMNO when
              nproofs <= batch_chunk, calldata grows linearly in nproofs.

Setup (deploy, mint, approve, startRoamingSession, UE funding) is not measured.
There is no off-chain proving step, so a plain leg's pipeline latency is just
its on-chain makespan. Compare with individual/aggregate_pipeline_latency_s of
zkroam_workload.py.

COLUMNS (per mode M in {individual, batch}):
  M_commit_*            commit leg   (total_tx, gas_used_per_tx, total_gas_used, ...)
  M_*                   settlement leg (same columns)
  M_gas_per_session     settlement gas / sessions
  M_e2e_gas_per_session (commit + settlement gas) / sessions
  M_valid               False if any tx of the mode's legs failed: do not compare it

ASSUMPTIONS
------------------------------------------------------------------
1. The CDR (sms, voiceSeconds, dataKB) and cost = sms*r.sms + voice*r.voice +
   data*r.data are a stand-in for what the real circuit proves; replace
   AgreementPlain.CDR/_cost and session_plan() with the circuit's rule.
2. The plain batch keeps the per-session UE commit: without it nothing binds the
   clear CDR to the UE and the VMNO could bill any usage up to the estimate.
3. After every leg the script waits for the txpool to drain and nonces are
   re-synced for any sender whose tx did not confirm.
4. Gas is read from receipts.
------------------------------------------------------------------
"""

import argparse
import csv
import math
import os
import statistics
import time
from collections import defaultdict
from types import SimpleNamespace

import yaml
from web3 import Web3

from besu_benchmark import (
    ResourceMonitor,
    build_rpc_endpoints,
    connect_web3,
    load_accounts,
    assign_accounts,
    load_json,
)
from agreement_common_e2e import (
    Env, Token, run_leg, settle_leg, estimate_gas, check_gas_fits_block,
    leg_columns, deploy_agreement, make_ue_accounts, fund_ue_accounts,
    session_plan, plain_cdr_hash, start_sessions, commit_leg,
)

MODES = ("individual", "batch")


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
        "mode": "all",          # individual | batch | all
    },
    "workload": {
        "num_vmnos": 8,
        # max CDRs per settleBatch tx; lower it if the gas guard trips
        "batch_chunk": 50,
        # agreement rates (per SMS, per voice second, per data KB)
        "rate_sms": 5,
        "rate_voice": 3,
        "rate_data": 2,
        # HMNO escrows cost * (1 + margin%) so every settlement refunds a bit
        "estimate_margin_pct": 20,
        # sessions started / UEs funded per confirmed chunk (setup only)
        "setup_chunk": 500,
        # native balance given to each UE; null = auto (gas_price * 150k * #commit txs)
        "ue_fund_wei": None,
    },
    "contracts": {
        "forge_out": "out",             # foundry output dir (forge build)
        "deployer_key_index": 0,        # accounts[i] that deploys everything
        "mno_key_indices": [0, 1],      # accounts[i] acting as mno1 / mno2
    },
    "execution": {
        "rate": 500.0,
        "workers": 32,
        "receipt_workers": 16,
        "receipt_timeout": 120.0,
        "poll_interval": 0.25,
        "latency_method": "block_time",
        "receipt_timeout_per_tx_s": 0.05,
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
        raise FileNotFoundError(f"Config file not found: {path}")
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
        batch_chunk=int(cfg["workload"]["batch_chunk"]),
        rates={"sms": int(cfg["workload"]["rate_sms"]),
               "voice": int(cfg["workload"]["rate_voice"]),
               "data": int(cfg["workload"]["rate_data"])},
        margin_pct=int(cfg["workload"]["estimate_margin_pct"]),
        setup_chunk=int(cfg["workload"]["setup_chunk"]),
        ue_fund_wei=(None if cfg["workload"]["ue_fund_wei"] is None
                     else int(cfg["workload"]["ue_fund_wei"])),
        forge_out=cfg["contracts"]["forge_out"],
        deployer_key_index=int(cfg["contracts"]["deployer_key_index"]),
        mno_key_indices=[int(x) for x in cfg["contracts"]["mno_key_indices"]],
        rate=float(cfg["execution"]["rate"]),
        workers=int(cfg["execution"]["workers"]),
        receipt_workers=int(cfg["execution"]["receipt_workers"]),
        receipt_timeout=float(cfg["execution"]["receipt_timeout"]),
        poll_interval=float(cfg["execution"]["poll_interval"]),
        latency_method=str(cfg["execution"]["latency_method"]),
        receipt_timeout_per_tx=float(cfg["execution"]["receipt_timeout_per_tx_s"]),
        drain_timeout_s=float(cfg["execution"]["drain_timeout_s"]),
        setup_per_sender=int(cfg["workload"].get("setup_per_sender", 4)),
        monitor_resources=bool(cfg["monitoring"]["enabled"]),
        monitor_interval=float(cfg["monitoring"]["interval"]),
        out_dir=cfg["output"]["out_dir"],
    )

    args.sweep = ([int(x) for x in args.sweep.split(",") if x.strip()]
                  if isinstance(args.sweep, str) else [int(x) for x in args.sweep])

    renamed = {"stateful": "individual", "stateless": None}
    if args.mode in renamed:
        raise ValueError(
            f"sweep.mode {args.mode!r} no longer exists: the plain workload now "
            f"settles real sessions on AgreementPlain. Use "
            f"{renamed[args.mode] or 'individual'!r} (or 'batch' / 'all').")
    if args.mode == "all":
        args.modes = list(MODES)
    elif args.mode in MODES:
        args.modes = [args.mode]
    else:
        raise ValueError(f"sweep.mode must be one of {MODES + ('all',)}, got {args.mode!r}")

    if args.latency_method not in ("block_time", "poll"):
        raise ValueError("execution.latency_method must be block_time or poll")
    if args.batch_chunk < 1:
        raise ValueError("workload.batch_chunk must be >= 1")
    if len(args.mno_key_indices) != 2 or args.mno_key_indices[0] == args.mno_key_indices[1]:
        raise ValueError("contracts.mno_key_indices must be two distinct account indices")
    return args


# ============================================================
# One mode: fresh agreement -> sessions -> commit leg -> settlement leg
# ============================================================

def run_mode(env, args, ctx, mode, nproofs, run, row):
    n_sessions = nproofs * args.num_vmnos
    plan = session_plan(args.num_vmnos, nproofs, args.rates, args.margin_pct)
    ue = ctx.ue_accts[:n_sessions]
    fund_ue_accounts(env, ctx.deployer, ue, ctx.ue_tx_budget, args.setup_chunk,
                     args.ue_fund_wei)

    env.phase(f"setup:{mode}")
    ag = deploy_agreement(env, "plain", ctx.deployer, ctx.mnos, args.rates,
                          ctx.token, args.forge_out)
    sessions = start_sessions(env, ag, ctx.mnos, ue, plan, args.setup_chunk)
    env.phase("idle")

    # ---- commit leg (UE -> submitCDR) ----
    commit = commit_leg(env, ag, ue, sessions, [plain_cdr_hash(p["cdr"]) for p in plan],
                        f"plain_{mode}_commit_n{nproofs}_vmnos{args.num_vmnos}_r{run}")

    # ---- settlement leg ----
    senders = ctx.accounts
    specs = []
    if mode == "individual":
        for u, p in enumerate(plan):
            specs.append({"sender": senders[u % len(senders)], "to": ag.address, "gas": 0,
                          "data": ag.contract.encode_abi(
                              abi_element_identifier="terminateRoamingSession",
                              args=[sessions[u][0], tuple(p["cdr"])])})
    else:
        k = 0
        for g in range(args.num_vmnos):
            base = g * nproofs
            for lo in range(0, nproofs, args.batch_chunk):
                idx = range(base + lo, base + min(lo + args.batch_chunk, nproofs))
                specs.append({"sender": senders[k % len(senders)], "to": ag.address, "gas": 0,
                              "data": ag.contract.encode_abi(
                                  abi_element_identifier="settleBatch",
                                  args=[[sessions[u][0] for u in idx],
                                        [tuple(plan[u]["cdr"]) for u in idx]])})
                k += 1

    # specs[0] is always a full-size tx (first chunk), i.e. the largest one
    gas = estimate_gas(env, senders[0]["address"], ag.address, specs[0]["data"], 1.15,
                       f"plain {mode} settlement")
    check_gas_fits_block(f"plain_{mode}", gas, env.web3s[0],
                         "Lower workload.batch_chunk.")
    for s in specs:
        s["gas"] = gas

    res = run_leg(env, f"plain_{mode}_n{nproofs}_vmnos{args.num_vmnos}_r{run}", specs)
    settle_leg(env, res)

    # ---- columns ----
    row.update(leg_columns(f"{mode}_commit", commit))
    row.update(leg_columns(mode, res))
    c_gas = row[f"{mode}_commit_total_gas_used"]
    s_gas = row[f"{mode}_total_gas_used"]
    row[f"{mode}_contract"] = ag.address
    row[f"{mode}_gas_per_session"] = s_gas / n_sessions if s_gas else None
    row[f"{mode}_e2e_gas_per_session"] = ((c_gas + s_gas) / n_sessions
                                          if c_gas and s_gas else None)
    row[f"{mode}_makespan_s"] = res.stats["confirm_duration_s"]
    # No off-chain proving/aggregation: pipeline == on-chain makespan.
    row[f"{mode}_pipeline_latency_s"] = res.stats["confirm_duration_s"]
    row[f"{mode}_valid_all"] = bool(row[f"{mode}_commit_valid"] and row[f"{mode}_valid"])
    print(f"  {mode}: settle tx={len(res.txs)} confirmed={res.stats['confirmed']} "
          f"gas/session={row[f'{mode}_gas_per_session']} "
          f"e2e/session={row[f'{mode}_e2e_gas_per_session']}")
    return commit, res


# ============================================================
# Driver
# ============================================================

def main():
    cli = argparse.ArgumentParser(
        description="Plain (no-ZK, no-aggregation) roaming settlement baseline "
                    "against AgreementPlain.sol. Configuration lives in the YAML file.")
    cli.add_argument("--config", default="config.plain.yml")
    args = load_config(cli.parse_args().config)

    os.makedirs(args.out_dir, exist_ok=True)
    detail_dir = os.path.join(args.out_dir, "detail")
    os.makedirs(detail_dir, exist_ok=True)

    monitor = None
    if args.monitor_resources:
        monitor = ResourceMonitor(args.monitor_interval)
        monitor.start()
        monitor.set_phase("setup")

    topology = load_json(args.topology)
    endpoints = build_rpc_endpoints(topology)
    web3s, chain_id = connect_web3(endpoints)
    accounts = load_accounts(args.accounts)
    if not accounts:
        raise RuntimeError("No workload accounts found.")
    assignment = assign_accounts(accounts, endpoints)

    env = Env(web3s, chain_id, accounts, assignment, endpoints, args)
    env.monitor = monitor

    deployer = accounts[args.deployer_key_index]
    mnos = [accounts[i] for i in args.mno_key_indices]
    token = Token(env, deployer, args.forge_out)
    for m in mnos:
        token.mint(env, deployer, m["address"], 10 ** 30)

    max_n = max(args.sweep) * args.num_vmnos
    print(f"generating {max_n} UE accounts...")
    ctx = SimpleNamespace(
        accounts=accounts, deployer=deployer, mnos=mnos, token=token,
        ue_accts=make_ue_accounts(max_n, f"plain-{int(time.time())}", len(web3s)),
        ue_tx_budget=len(args.modes) * args.runs * len(args.sweep) + 2,
    )
    if monitor is not None:
        monitor.set_phase("idle")

    print(f"\nworkload.num_vmnos={args.num_vmnos} modes={args.modes} "
          f"batch_chunk={args.batch_chunk} latency={args.latency_method}")

    rows = []
    for nproofs in args.sweep:
        for run in range(1, args.runs + 1):
            print("\n" + "=" * 60)
            print(f"nproofs/vmno={nproofs}  num_vmnos={args.num_vmnos}  run={run}")
            print("=" * 60)
            row = {"nproofs_per_vmno": nproofs, "num_vmnos": args.num_vmnos, "run": run,
                   "batch_chunk": args.batch_chunk,
                   "sessions_total": nproofs * args.num_vmnos}
            for mode in args.modes:
                commit, res = run_mode(env, args, ctx, mode, nproofs, run, row)
                write_leg_detail(detail_dir, f"{mode}_commit", nproofs, args, run,
                                 commit.txs, endpoints)
                write_leg_detail(detail_dir, mode, nproofs, args, run, res.txs, endpoints)
            row["all_legs_valid"] = all(row.get(f"{m}_valid_all") for m in args.modes)
            rows.append(row)

    stem = (f"{args.experiment_name}_{args.num_vmnos}_{args.rate}_{args.workers}_"
            f"{args.receipt_workers}")
    if monitor is not None:
        monitor.stop()
        monitor.write_csv(os.path.join(args.out_dir, f"{stem}_resource_usage.csv"))
    write_sweep_summary(os.path.join(args.out_dir, f"{stem}_sweep_summary.csv"), rows)
    print_highlights(rows, args.modes)


# ============================================================
# Output
# ============================================================

def write_leg_detail(detail_dir, leg, nproofs, args, run, txs, endpoints):
    path = os.path.join(
        detail_dir,
        f"plain_{leg}_n{nproofs}_{args.num_vmnos}_{args.rate}_{args.workers}_"
        f"{args.receipt_workers}_r{run}.csv")
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
            w.writerow({
                "tx_index": t["tx_index"], "sender": t["sender"],
                "rpc_node": endpoints[t["rpc_index"]]["name"],
                "tx_hash": t["tx_hash"], "nonce": t["nonce"],
                "submit_ts": t["submit_ts"], "confirm_ts": t["confirm_ts"],
                "latency_s": lat if lat is not None else poll_lat,
                "poll_latency_s": poll_lat, "block_number": t["block_number"],
                "block_ts": t.get("block_ts"), "gas_used": t.get("gas_used"),
                "status": t["status"], "error": t["error"],
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
        w.writerows({k: ("" if v is None else v) for k, v in r.items()} for r in rows)
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
    print("\n" + "=" * 110)
    print("PLAIN BASELINE HIGHLIGHTS (median across runs)")
    print("=" * 110)
    hdr = f"{'nproofs':>8} {'vmnos':>6}"
    for m in modes:
        hdr += f" | {m + ' tx':>12} {'settle/sess':>12} {'e2e/sess':>10} {'makespan':>9}"
    print(hdr)
    for n in sorted(by_n):
        rs = by_n[n]
        line = f"{n:>8} {rs[0]['num_vmnos']:>6}"
        for m in modes:
            line += (f" | {_fmt(_med(rs, f'{m}_total_tx'), '>12,.0f')} "
                     f"{_fmt(_med(rs, f'{m}_gas_per_session'), '>12,.0f')} "
                     f"{_fmt(_med(rs, f'{m}_e2e_gas_per_session'), '>10,.0f')} "
                     f"{_fmt(_med(rs, f'{m}_makespan_s'), '>8.1f')}s")
            if not all(r.get(f"{m}_valid_all") for r in rs):
                line += " [INVALID: failed txs]"
        print(line)
    print("=" * 110)


if __name__ == "__main__":
    main()
