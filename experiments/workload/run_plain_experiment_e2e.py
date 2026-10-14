#!/usr/bin/env python3
"""
Full automated PLAIN-baseline sweep (counterpart of run_zkroam_experiment_e2e.py):
  forge build -> network up -> run plain_workload_e2e.py -> save artifacts -> network down

For every (network, num_vmnos) pair it writes results to
  <results-root>/plain-<network>-<N>vmnos/
containing: config.yml (the exact config used), run.log (full console output),
topology.json, deployed_contracts.json, and whatever plain_workload_e2e.py writes.

AgreementPlain (and the BenchToken helper) are compiled once up front with
`forge build` and deployed by plain_workload_e2e.py itself, FRESH for every leg, with the
same escrow / session / payout logic as the zkRoam agreements. The zkRoam verifiers
are NOT deployed unless you pass --with-verifiers.

Examples:
  python3 workload/run_plain_experiment_e2e.py --network wan --num-vmnos 64
  python3 workload/run_plain_experiment_e2e.py --network wan baseline heterogeneous --num-vmnos 16 64 256
  python3 workload/run_plain_experiment_e2e.py --network wan --num-vmnos 64 --nproofs 8 64 128 --mode batch
  python3 workload/run_plain_experiment_e2e.py --network wan --num-vmnos 64 --reuse-network
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
# ------------------------------------------------------------------
# helpers shared with run-experiment.py
# ------------------------------------------------------------------

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


# ------------------------------------------------------------------
# contract build (once, before any network starts)
# ------------------------------------------------------------------

REQUIRED_ARTIFACTS = [
    ("AgreementPlain.sol", "AgreementPlain"),
    ("BenchToken.sol", "BenchToken"),
]


def build_contracts(forge_out, skip_build):
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


# ------------------------------------------------------------------
# network + sweep
# ------------------------------------------------------------------

def start_network(network, args, log):
    run(["bash", NETWORK_SCRIPTS[network]], log)
    wait_for_chain()
    if args.with_verifiers:
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
    out_dir_rel = os.path.join(args.results_root, f"plain-{network}-{n_vmnos}vmnos_e2e")
    out_dir = os.path.join(ROOT, out_dir_rel)

    if os.path.exists(os.path.join(out_dir, "run.log")) and not args.force:
        print(f"SKIP {out_dir_rel} (exists; use --force to rerun)")
        return "skipped"
    os.makedirs(out_dir, exist_ok=True)

    cfg = json.loads(json.dumps(tmpl))  # deep copy
    cfg.setdefault("experiment", {})["name"] = f"plain_{network}_{n_vmnos}vmnos"
    cfg.setdefault("workload", {})["num_vmnos"] = n_vmnos
    cfg.setdefault("output", {})["out_dir"] = out_dir_rel
    cfg.setdefault("contracts", {})["forge_out"] = args.forge_out
    cfg.setdefault("sweep", {})
    if args.nproofs:
        cfg["sweep"]["nproofs"] = args.nproofs
    if args.mode:
        cfg["sweep"]["mode"] = args.mode
    if args.batch_chunk:
        cfg["workload"]["batch_chunk"] = args.batch_chunk
    cfg_path = os.path.join(out_dir, "config.yml")
    with open(cfg_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

    status = "ok"
    with open(os.path.join(out_dir, "run.log"), "w") as log:
        try:
            if not args.reuse_network:
                start_network(network, args, log)
            print(f"Running plain sweep with {n_vmnos} vmnos "
                  f"(see {out_dir_rel}/run.log)...", flush=True)
            run([sys.executable, "workload/plain_workload_e2e.py",
                 "--config", os.path.relpath(cfg_path, ROOT)], log)

            # Sanity check: the workload must have produced its summary CSV.
            produced = [f for f in os.listdir(out_dir) if f.endswith("_sweep_summary.csv")]
            if not produced:
                raise RuntimeError("workload exited 0 but wrote no *_sweep_summary.csv")

            check_all_legs_valid(out_dir)

            # Snapshot the setup that produced these results
            for src in ("networkFiles/topology.json",
                        cfg["contracts"].get("deployed_contracts", "deployed_contracts.json")):
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
    ap.add_argument("--config", default="workload/config.plain.yml",
                    help="template config for plain_workload_e2e.py")
    ap.add_argument("--nproofs", nargs="+", type=int, help="override sweep.nproofs")
    ap.add_argument("--mode", choices=["individual", "batch", "all"],
                    help="override sweep.mode")
    ap.add_argument("--batch-chunk", type=int, help="override workload.batch_chunk")
    ap.add_argument("--results-root", default="results")
    ap.add_argument("--forge-out", default="out",
                    help="foundry output dir holding out/<File>.sol/<Contract>.json")
    ap.add_argument("--no-build", action="store_true",
                    help="skip `forge build` (artifacts must already exist)")
    ap.add_argument("--with-verifiers", action="store_true",
                    help="also run deploy_verifiers.py after the network starts")
    ap.add_argument("--reuse-network", action="store_true",
                    help="don't start/stop the network; use the one already running")
    ap.add_argument("--keep-network", action="store_true",
                    help="leave the network up after the last run")
    ap.add_argument("--save-node-logs", action="store_true",
                    help="copy logs/nodeXX into the results folder")
    ap.add_argument("--force", action="store_true", help="rerun even if results exist")
    args = ap.parse_args()

    with open(os.path.join(ROOT, args.config)) as f:
        tmpl = yaml.safe_load(f) or {}

    # Build once up front so a missing compiler fails before any network starts.
    build_contracts(args.forge_out, args.no_build)

    summary = []
    for network in args.network:
        for n in args.num_vmnos:
            print(f"\n{'=' * 70}\n  plain | {network} | num_vmnos={n}\n{'=' * 70}", flush=True)
            summary.append((network, n, run_sweep(args, network, n, tmpl)))

    print("\n" + "=" * 70 + "\nSUMMARY")
    for network, n, status in summary:
        print(f"  {network:14s} vmnos={n:<5d} {status}")
    sys.exit(0 if all(s in ("ok", "skipped") for _, _, s in summary) else 1)


if __name__ == "__main__":
    main()