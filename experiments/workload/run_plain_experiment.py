#!/usr/bin/env python3
"""
Full automated PLAIN-baseline sweep (counterpart of run-experiment.py):
  compile contract -> network up -> run plain_workload.py -> save artifacts -> network down

For every (network, num_vmnos) pair it writes results to
  <results-root>/plain-<network>-<N>vmnos/
containing: config.yml (the exact config used), run.log (full console output),
topology.json, deployed_contracts.json, and whatever plain_workload.py writes.

The plain contract is deployed by plain_workload.py itself (from the compiled
artifact) on every fresh network, and its address is written into
deployed_contracts.json under "plain_settlement". The zkRoam verifiers are NOT
deployed unless you pass --with-verifiers.

Examples:
  python3 workload/run_plain_experiment.py --network wan --num-vmnos 64
  python3 workload/run_plain_experiment.py --network wan baseline heterogeneous --num-vmnos 16 64 256
  python3 workload/run_plain_experiment.py --network wan --num-vmnos 64 --nproofs 8 64 128 --mode batch
  python3 workload/run_plain_experiment.py --network wan --num-vmnos 64 --reuse-network
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
CONTRACT_NAME = "PlainRoamingSettlement"
# Pinned to 0.8.19: the contract's pragma floor, and its default EVM target
# (paris) has no PUSH0, so the bytecode also runs on chains whose genesis
# predates Shanghai. 0.8.20+ would emit PUSH0 by default.
DEFAULT_SOLC_VERSION = "0.8.19"


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
# contract compilation (only needed once, before any network starts)
# ------------------------------------------------------------------

def artifact_from_combined_json(data, contract_name=CONTRACT_NAME):
    """solc --combined-json abi,bin output -> {"abi": [...], "bytecode": "0x..."}."""
    for key, c in data["contracts"].items():
        if key.split(":")[-1] == contract_name:
            abi = c["abi"]
            if isinstance(abi, str):        # older solc emits the ABI as a JSON string
                abi = json.loads(abi)
            bc = c["bin"]
            return {"abi": abi, "bytecode": bc if bc.startswith("0x") else "0x" + bc}
    raise RuntimeError(f"{contract_name} not found in compiler output "
                       f"(keys: {list(data['contracts'])})")


def latest_block_gas_limit(w3):
    res = w3.provider.make_request("eth_getBlockByNumber", ["latest", False])
    if res.get("error"):
        raise RuntimeError(f"eth_getBlockByNumber failed: {res['error']}")
    return int(res["result"]["gasLimit"], 16)



def compile_contract(src, artifact, force=False, solc_version=DEFAULT_SOLC_VERSION):
    """Compile src -> artifact JSON (abi + bytecode). Tries solc, then py-solc-x."""
    src_abs = os.path.join(ROOT, src)
    art_abs = os.path.join(ROOT, artifact)

    if os.path.exists(art_abs) and not force:
        print(f"Using existing artifact {artifact} (use --recompile to rebuild).")
        return
    if not os.path.exists(src_abs):
        raise RuntimeError(f"contract source not found: {src} (set --contract-src)")

    os.makedirs(os.path.dirname(art_abs), exist_ok=True)
    data = None

    if shutil.which("solc"):
        print(f"Compiling {src} with solc...", flush=True)
        r = subprocess.run(
            ["solc", "--combined-json", "abi,bin", "--optimize", "--optimize-runs", "200", src],
            cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"solc failed:\n{r.stderr}")
        data = json.loads(r.stdout)
    else:
        try:
            import solcx  # pip install py-solc-x
        except ImportError:
            raise RuntimeError(
                "No Solidity compiler found. Install solc (or pip install py-solc-x "
                "plus solcx.install_solc('0.8.24')), or compile "
                f"{src} yourself and pass --artifact <json with abi+bytecode>.")
        installed = {str(v) for v in solcx.get_installed_solc_versions()}
        if solc_version not in installed:
            print(f"Installing solc {solc_version} via py-solc-x "
                  f"(needs internet, one-time)...", flush=True)
            solcx.install_solc(solc_version)
        print(f"Compiling {src} with py-solc-x (solc {solc_version})...", flush=True)
        out = solcx.compile_files([src_abs], output_values=["abi", "bin"],
                                  optimize=True, optimize_runs=200,
                                  solc_version=solc_version)
        data = {"contracts": {k: {"abi": v["abi"], "bin": v["bin"]} for k, v in out.items()}}

    with open(art_abs, "w") as f:
        json.dump(artifact_from_combined_json(data), f, indent=2)
    print(f"Wrote {artifact}")


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


def run_sweep(args, network, n_vmnos, tmpl):
    out_dir_rel = os.path.join(args.results_root, f"plain-{network}-{n_vmnos}vmnos")
    out_dir = os.path.join(ROOT, out_dir_rel)

    if os.path.exists(os.path.join(out_dir, "run.log")) and not args.force:
        print(f"SKIP {out_dir_rel} (exists; use --force to rerun)")
        return "skipped"
    os.makedirs(out_dir, exist_ok=True)

    cfg = json.loads(json.dumps(tmpl))  # deep copy
    cfg.setdefault("experiment", {})["name"] = f"plain_{network}_{n_vmnos}vmnos"
    cfg.setdefault("workload", {})["num_vmnos"] = n_vmnos
    cfg.setdefault("output", {})["out_dir"] = out_dir_rel
    cfg.setdefault("contracts", {})["plain_artifact"] = args.artifact
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
            run([sys.executable, "workload/plain_workload.py",
                 "--config", os.path.relpath(cfg_path, ROOT)], log)

            # Sanity check: the workload must have produced its summary CSV.
            produced = [f for f in os.listdir(out_dir) if f.endswith("_sweep_summary.csv")]
            if not produced:
                raise RuntimeError("workload exited 0 but wrote no *_sweep_summary.csv")

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
                    help="template config for plain_workload.py")
    ap.add_argument("--nproofs", nargs="+", type=int, help="override sweep.nproofs")
    ap.add_argument("--mode", choices=["stateless", "stateful", "batch", "all"],
                    help="override sweep.mode")
    ap.add_argument("--batch-chunk", type=int, help="override workload.batch_chunk")
    ap.add_argument("--results-root", default="results")
    ap.add_argument("--contract-src", default="contracts/PlainRoamingSettlement.sol")
    ap.add_argument("--artifact", default="build/PlainRoamingSettlement.json",
                    help="compiled JSON (abi + bytecode); built from --contract-src if missing")
    ap.add_argument("--recompile", action="store_true", help="rebuild the artifact")
    ap.add_argument("--solc-version", default=DEFAULT_SOLC_VERSION,
                    help="solc version used by the py-solc-x fallback (auto-installed)")
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

    # Compile once up front so a missing compiler fails before any network starts.
    compile_contract(args.contract_src, args.artifact, force=args.recompile,
                     solc_version=args.solc_version)

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