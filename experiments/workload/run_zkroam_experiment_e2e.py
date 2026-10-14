#!/usr/bin/env python3
"""
Full automated zkRoam sweep (real settlement contracts):
  forge build -> network up -> deploy verifier + anchor
  -> run nproofs sweep (individual: commit + terminateRoamingSession per session;
                        aggregate: anchor attestation + settleBatch per VMNO)
  -> save artifacts -> network down

AgreementIndividual / AgreementAggregate and the benchmark helpers (BenchToken,
BenchGroth16Verifier) are compiled once up front with `forge build` and deployed by
zkroam_workload.py itself, FRESH for every leg. Only the verifier (cdr_verifier) and
the anchor (aggregate_anchor) are deployed here by deploy_verifiers.py.

For every (network, num_vmnos) pair it writes results to
  <results-root>/zkroam-<network>-<N>vmnos/
containing: config.yml (the exact config used), run.log (full console output),
topology.json, deployed_contracts.json, and whatever zkroam_workload.py writes.

The individual path always includes the per-session commit leg (the contract
reverts without a committed CDR hash); the aggregate path never does.

Examples:
  python3 workload/run_zkroam_experiment_e2e.py --network wan --num-vmnos 64
  python3 workload/run_zkroam_experiment_e2e.py --network wan baseline heterogeneous --num-vmnos 16 64 256
  python3 workload/run_zkroam_experiment_e2e.py --network wan --num-vmnos 64 --nproofs 8 64 128
  python3 workload/run_zkroam_experiment_e2e.py --network wan --num-vmnos 64 --mode aggregate
  python3 workload/run_zkroam_experiment_e2e.py --network wan --num-vmnos 64 --no-build
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import yaml  # pip install pyyaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NETWORK_SCRIPTS = {
    "wan":           "scripts/04a-run-network-wan.sh",
    "baseline":      "scripts/04b-run-baseline.sh",
    "heterogeneous": "scripts/04-run-network.sh",
}
RPC_URL = "http://localhost:8545"
DEPLOYED_CONTRACTS = "deployed_contracts.json"  # written by deploy_verifiers.py


def run(cmd, log, check=True):
    """Run a command from ROOT, streaming output to console and the log file."""
    log.write(f"\n$ {' '.join(cmd)}\n")
    log.flush()
    print(f"\n$ {' '.join(cmd)}", flush=True)
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in p.stdout:
        sys.stdout.write(line)
        log.write(line)
    p.wait()
    log.flush()
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed (exit {p.returncode}): {' '.join(cmd)}")
    return p.returncode


def rpc_block_number():
    req = urllib.request.Request(
        RPC_URL,
        data=json.dumps({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return int(json.load(r)["result"], 16)


def wait_for_chain(timeout=240):
    """Wait until node00 answers RPC AND the chain is producing blocks."""
    print(f"Waiting for chain to produce blocks (up to {timeout}s)...", flush=True)
    start = time.time()
    while time.time() - start < timeout:
        try:
            if rpc_block_number() >= 2:
                print(f"Chain is live ({time.time() - start:.0f}s).", flush=True)
                return
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError("chain did not start producing blocks in time")


def teardown(log):
    run(["docker", "compose", "down", "-v", "--remove-orphans"], log, check=False)


REQUIRED_ARTIFACTS = [
    ("AgreementIndividual.sol", "AgreementIndividual"),
    ("AgreementAggregate.sol", "AgreementAggregate"),
    ("BenchToken.sol", "BenchToken"),
    ("BenchGroth16Verifier.sol", "BenchGroth16Verifier"),
    ("CDRVerifier.sol", "Groth16Verifier"),
    ("SnarkPackAggregateAnchor.sol", "SnarkPackAggregateAnchor"),
]


def build_contracts(forge_out, skip_build):
    """forge build once, before any network starts, then check the artifacts the
    workload will load (out/<File>.sol/<Contract>.json)."""
    if not skip_build:
        if not shutil.which("forge"):
            raise RuntimeError("`forge` not found on PATH (install Foundry, or build "
                               "yourself and pass --no-build)")
        print("Building contracts with forge...", flush=True)
        r = subprocess.run(["forge", "build"], cwd=ROOT, capture_output=True, text=True)
        sys.stdout.write(r.stdout[-2000:])
        if r.returncode != 0:
            raise RuntimeError(f"forge build failed:\n{r.stderr or r.stdout}")
    missing = [f"{forge_out}/{src}/{name}.json" for src, name in REQUIRED_ARTIFACTS
               if not os.path.exists(os.path.join(ROOT, forge_out, src, f"{name}.json"))]
    if missing:
        raise RuntimeError("missing compiled artifacts (check src/out in foundry.toml): "
                           + ", ".join(missing))


def start_network(network, log):
    run(["bash", NETWORK_SCRIPTS[network]], log)
    wait_for_chain()
    run([sys.executable, "workload/deploy_verifiers.py",
         "--topology", "networkFiles/topology.json",
         "--accounts", "accounts/accounts.json"], log)


def check_all_legs_valid(out_dir):
    """A run whose txs reverted/timed out is not a result: surface it as FAILED."""
    import csv
    bad = []
    for f in os.listdir(out_dir):
        if f.endswith("_sweep_summary.csv"):
            with open(os.path.join(out_dir, f)) as fh:
                for row in csv.DictReader(fh):
                    if row.get("all_legs_valid", "True") != "True":
                        bad.append(f"nproofs={row.get('nproofs_per_vmno')} run={row.get('run')}")
    if bad:
        raise RuntimeError("some legs had failed txs, results invalid: " + "; ".join(bad))


def run_sweep(args, network, n_vmnos, tmpl):
    out_dir_rel = os.path.join(args.results_root, f"zkroam-{network}-{n_vmnos}vmnos_e2e")
    out_dir = os.path.join(ROOT, out_dir_rel)

    if os.path.exists(os.path.join(out_dir, "run.log")) and not args.force:
        print(f"SKIP {out_dir_rel} (exists; use --force to rerun)")
        return "skipped"
    os.makedirs(out_dir, exist_ok=True)

    cfg = json.loads(json.dumps(tmpl))  # deep copy
    cfg["experiment"]["name"] = f"zkroam_{network}_{n_vmnos}vmnos"
    cfg.setdefault("workload", {})
    cfg["workload"]["num_vmnos"] = n_vmnos
    cfg.setdefault("contracts", {})["forge_out"] = args.forge_out
    cfg["output"]["out_dir"] = out_dir_rel
    if args.nproofs:
        cfg["sweep"]["nproofs"] = args.nproofs
    if args.mode:
        cfg["sweep"]["mode"] = args.mode
    if args.num_attesters:
        cfg["workload"]["num_attesters"] = args.num_attesters
    cfg_path = os.path.join(out_dir, "config.yml")
    with open(cfg_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

    deployed_rel = cfg.get("contracts", {}).get("deployed_contracts", DEPLOYED_CONTRACTS)

    status = "ok"
    with open(os.path.join(out_dir, "run.log"), "w") as log:
        try:
            if not args.reuse_network:
                start_network(network, log)
            print(f"Running zkRoam sweep with {n_vmnos} vmnos (see {out_dir_rel}/run.log)...", flush=True)
            run([sys.executable, "workload/zkroam_workload_e2e.py",
                 "--config", os.path.relpath(cfg_path, ROOT)], log)

            check_all_legs_valid(out_dir)

            # Snapshot the setup that produced these results
            for src in ("networkFiles/topology.json", deployed_rel):
                if os.path.exists(os.path.join(ROOT, src)):
                    shutil.copy(os.path.join(ROOT, src), out_dir)
            if args.save_node_logs and os.path.isdir(os.path.join(ROOT, "logs")):
                shutil.copytree(os.path.join(ROOT, "logs"),
                                os.path.join(out_dir, "node_logs"), dirs_exist_ok=True)
        except Exception as e:
            status = f"FAILED: {e}"
            log.write(f"\n{status}\n")
            print(f"\n{status}", file=sys.stderr)
        finally:
            if not args.reuse_network and not args.keep_network:
                teardown(log)
    return status


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--network", nargs="+", required=True,
                    choices=list(NETWORK_SCRIPTS), help="one or more network configs")
    ap.add_argument("--num-vmnos", nargs="+", type=int, required=True,
                    help="one or more num_vmnos values")
    ap.add_argument("--config", default="workload/config.yml", help="template config")
    ap.add_argument("--nproofs", nargs="+", type=int, help="override sweep.nproofs")
    ap.add_argument("--mode", choices=["individual", "aggregate", "both"],
                    help="override sweep.mode")
    ap.add_argument("--num-attesters", type=int,
                    help="override workload.num_attesters (default 4 in config.yml)")
    ap.add_argument("--forge-out", default="out",
                    help="foundry output dir holding out/<File>.sol/<Contract>.json")
    ap.add_argument("--no-build", action="store_true",
                    help="skip `forge build` (artifacts must already exist)")
    ap.add_argument("--results-root", default="results")
    ap.add_argument("--reuse-network", action="store_true",
                    help="don't start/stop the network; use the one already running")
    ap.add_argument("--keep-network", action="store_true",
                    help="leave the network up after the last run")
    ap.add_argument("--save-node-logs", action="store_true",
                    help="copy logs/nodeXX into the results folder")
    ap.add_argument("--force", action="store_true", help="rerun even if results exist")
    args = ap.parse_args()

    with open(os.path.join(ROOT, args.config)) as f:
        tmpl = yaml.safe_load(f)

    build_contracts(args.forge_out, args.no_build)   # fail before any network starts

    summary = []
    for network in args.network:
        for n in args.num_vmnos:
            print(f"\n{'=' * 70}\n  {network} | num_vmnos={n}\n{'=' * 70}", flush=True)
            summary.append((network, n, run_sweep(args, network, n, tmpl)))

    print("\n" + "=" * 70 + "\nSUMMARY")
    for network, n, status in summary:
        print(f"  {network:14s} vmnos={n:<5d} {status}")
    sys.exit(0 if all(s in ("ok", "skipped") for _, _, s in summary) else 1)


if __name__ == "__main__":
    main()