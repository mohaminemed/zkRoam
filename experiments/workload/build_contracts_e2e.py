#!/usr/bin/env python3
"""
build_contracts_e2e.py - compile the benchmark contracts with Python (py-solc-x) and
write Foundry-layout artifacts that the workloads load:

    <out>/<File>.sol/<Contract>.json   ->  {"abi": [...], "bytecode": {"object": "0x..."}}

Used by run_zkroam_experiment_e2e.py / run_plain_experiment_e2e.py (--compiler python, the
default). Can also be run on its own:

    python3 workload/build_contracts_e2e.py --contracts-dir contracts --out out

Source files are located by NAME anywhere under --contracts-dir (recursively), so it
does not matter whether the Agreement* files sit next to Agreement.sol or in a
subfolder; their relative imports (./AgreementBase.sol, ../CDRVerifier.sol) resolve
from wherever they are.

OpenZeppelin is found in node_modules/@openzeppelin/contracts or
lib/openzeppelin-contracts/contracts (or pass --oz-path).

EVM target: default "paris" (no PUSH0), so the bytecode also runs on chains whose
genesis predates Shanghai. OpenZeppelin 5.x needs solc >= 0.8.20, which would
otherwise emit PUSH0. Use --evm-version default only if your chain supports it.
"""
import argparse
import json
import os
import sys

DEFAULT_SOLC = "0.8.24"

# source file -> contracts we need artifacts for
ALL_TARGETS = {
    "AgreementIndividual.sol": ["AgreementIndividual"],
    "AgreementAggregate.sol": ["AgreementAggregate"],
    "AgreementPlain.sol": ["AgreementPlain"],
    "BenchToken.sol": ["BenchToken"],
    "BenchGroth16Verifier.sol": ["BenchGroth16Verifier"],
    "CDRVerifier.sol": ["Groth16Verifier"],
    "SnarkPackAggregateAnchor.sol": ["SnarkPackAggregateAnchor"],
}


def find_sources(contracts_dir, names):
    found, dupes = {}, {}
    for dp, _dn, fs in os.walk(contracts_dir):
        if "node_modules" in dp.split(os.sep):
            continue
        for f in fs:
            if f in names:
                dupes.setdefault(f, []).append(os.path.join(dp, f))
    missing = [n for n in names if n not in dupes]
    if missing:
        raise RuntimeError(f"source file(s) not found under {contracts_dir}: {', '.join(missing)}")
    for n, paths in dupes.items():
        if len(paths) > 1:
            raise RuntimeError(f"{n} found more than once: {paths} (keep a single copy: "
                               f"duplicate contract names make artifacts ambiguous)")
        found[n] = os.path.abspath(paths[0])
    return found


def find_openzeppelin(root, explicit=None):
    """Look in the project and then in every parent folder (monorepo layouts)."""
    if explicit:
        cands = [explicit]
    else:
        cands, d = [], os.path.abspath(root)
        while True:
            cands += [os.path.join(d, "node_modules", "@openzeppelin", "contracts"),
                      os.path.join(d, "lib", "openzeppelin-contracts", "contracts"),
                      os.path.join(d, "node_modules", "openzeppelin-contracts", "contracts")]
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    for c in cands:
        if os.path.isfile(os.path.join(c, "token", "ERC20", "ERC20.sol")):
            print(f"OpenZeppelin: {c}")
            return os.path.abspath(c)
    raise RuntimeError("OpenZeppelin contracts (token/ERC20/ERC20.sol) not found; looked in "
                       f"{cands[:3]}{' and parent folders' if not explicit else ''}. "
                       "Install it (npm i @openzeppelin/contracts) or pass --oz-path.")


def build(root, contracts_dir="contracts", out="out", oz_path=None, solc_version=DEFAULT_SOLC,
          evm_version="paris", targets=None, optimize_runs=200):
    """Compile `targets` (default ALL_TARGETS) and write artifacts. Returns written paths."""
    import solcx  # pip install py-solc-x

    targets = targets or ALL_TARGETS
    root = os.path.abspath(root)
    sources = find_sources(os.path.join(root, contracts_dir), list(targets))
    oz = find_openzeppelin(root, oz_path)

    installed = {str(v) for v in solcx.get_installed_solc_versions()}
    if solc_version not in installed:
        print(f"Installing solc {solc_version} via py-solc-x (needs internet, one-time)...",
              flush=True)
        solcx.install_solc(solc_version)

    print(f"Compiling {len(sources)} files with solc {solc_version} "
          f"(evm={evm_version}, optimizer runs={optimize_runs})...", flush=True)
    # solc resolves import targets relative to --base-path, so the remapping target must
    # be RELATIVE to it (an absolute target is read as a virtual file name and fails with
    # "File not found. Searched ... <base>"). Use the closest common folder of the project
    # and OpenZeppelin as base, so OZ may live inside the project or above it.
    base = os.path.commonpath([root, oz])
    kwargs = dict(
        output_values=["abi", "bin"],
        import_remappings={"@openzeppelin/contracts": os.path.relpath(oz, base)},
        base_path=base,
        allow_paths=",".join([root, oz, base]),
        optimize=True, optimize_runs=optimize_runs,
        solc_version=solc_version,
    )
    if evm_version and evm_version != "default":
        kwargs["evm_version"] = evm_version
    compiled = solcx.compile_files(list(sources.values()), **kwargs)

    out_abs = os.path.join(root, out)
    by_file = {os.path.abspath(p): n for n, p in sources.items()}
    written = []
    for key, c in compiled.items():
        path, _, name = key.rpartition(":")
        fname = by_file.get(os.path.abspath(path if os.path.isabs(path)
                                            else os.path.join(base, path)))
        if fname is None or name not in targets[fname]:
            continue
        if not c["bin"]:
            raise RuntimeError(f"{name} compiled to empty bytecode (abstract?)")
        d = os.path.join(out_abs, fname)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{name}.json")
        with open(p, "w") as f:
            json.dump({"abi": c["abi"], "bytecode": {"object": "0x" + c["bin"]}}, f)
        written.append(p)

    want = {(f, n) for f, ns in targets.items() for n in ns}
    got = {(os.path.basename(os.path.dirname(p)), os.path.basename(p)[:-5]) for p in written}
    if want - got:
        raise RuntimeError(f"no artifact produced for: {sorted(want - got)}")
    print(f"Wrote {len(written)} artifacts under {out}/")
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--contracts-dir", default="contracts")
    ap.add_argument("--out", default="out")
    ap.add_argument("--oz-path")
    ap.add_argument("--solc-version", default=DEFAULT_SOLC)
    ap.add_argument("--evm-version", default="paris")
    ap.add_argument("--only", nargs="+", choices=sorted(ALL_TARGETS),
                    help="compile only these source files")
    a = ap.parse_args()
    t = {k: ALL_TARGETS[k] for k in a.only} if a.only else None
    try:
        build(a.root, a.contracts_dir, a.out, a.oz_path, a.solc_version, a.evm_version, t)
    except RuntimeError as e:
        sys.exit(f"build failed: {e}")


if __name__ == "__main__":
    main()