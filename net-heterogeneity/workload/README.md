# Workload Experiments

Benchmarks the **on-chain** cost of a zkRoam CDR roaming settlement, comparing
two strategies at the same per-VMNO proof-count sweep the Rust pipeline
(`aggregation/`) already measures off-chain:

- **individual** — post every Groth16 CDR proof as its own transaction
  (no aggregation).
- **aggregate** — aggregate off-chain with SnarkPack, anchor the result
  on-chain with one attestation per VMNO settlement.

It's built as a workload layered on top of a generic Besu QBFT
transaction-benchmark harness (`besu_benchmark.py`), submitted through the
same producer/poller infrastructure, so gas, throughput, and confirmation
latency are all real measurements against a live Besu network — not
estimates, wherever a contract is actually deployed.

- `besu_benchmark.py`: a generic rate-controlled transaction producer and receipt poller.
- `zkroam_workload.py`: a zkRoam-specific sweep comparing individual Groth16 verification transactions with one aggregate-anchor transaction per VMNO.

The scripts submit transactions to the RPC endpoints listed in `networkFiles/topology.json`, use accounts from `accounts/accounts.json`, and write measurements under `results/`.

---

## File map

| File | What it is |
|---|---|
| `besu_benchmark.py` | Generic tx-submission/receipt-polling harness (rate-controlled producer, resource monitor, per-RPC stats). Everything else imports from this. |
| `zkroam_workload.py` | The actual workload: sweeps `nproofs`, builds individual/aggregate transactions, writes `sweep_summary.csv`. |
| `contracts/CDRVerifier.sol` | Real snarkjs-generated Groth16 verifier (BN254) for individual proofs. |
| `contracts/SnarkPackAggregateAnchor.sol` | Attestation/anchor contract for aggregate results (see "Why not a real aggregate verifier?" below). |
| `deploy_verifiers.py` | Compiles + deploys both contracts, authorizes a verifier pool. |
| `verify_real_proof.py` | Standalone smoke test: deploy + call `verifyProof()` with a real proof, assert `true`, before running the full sweep. |
| `config.yml` | Full config schema with inline comments. Rdit and run. |
| `fixtures/` | Real snarkjs `proof.json`/`public.json`. |

---

## Important: run from the network directory

The bundled configuration uses paths relative to `net-heterogeneity/`, not relative to this `workload/` directory. Start every command from the network directory:

```bash
cd zkRoam/net-heterogeneity
```

When using the zkRoam driver, pass the configuration explicitly:

```bash
python3 workload/zkroam_workload.py --config workload/config.yml
```

---

## Prerequisites

Start the Besu network before submitting workload transactions. The network setup and tier profiles are documented in [`../README.md`](../README.md).

Install the Python dependencies in a virtual environment:

```bash
cd net-heterogeneity
python3 -m venv venv
. venv/bin/activate
python -m pip install --upgrade pip
python -m pip install web3 pyyaml py-solc-x psutil
```


For contract deployment with `deploy_verifiers.py`, install Solidity compiler support and download the configured compiler:

```bash
python -m pip install py-solc-x
python -c "import solcx; solcx.install_solc('0.8.19')"
```

The script can also use a locally installed compiler through the `SOLC_BINARY` environment variable when downloading a compiler is not possible.

You need:
- A running Besu QBFT network, with `networkFiles/topology.json` (RPC
  endpoints) and an `accounts.json` (funded keypairs) matching the format
  `besu_benchmark.py` expects.
- (Optional but strongly recommended) real snarkjs `proof.json`/
  `public.json` for the circuit `CDRVerifier.sol` was actually compiled
  from — `fixtures/` has a working example set.

---

## Quick start

```bash
# 1. Deploy both contracts + authorize a verifier
python3 deploy_verifiers.py \
  --topology networkFiles/topology.json \
  --accounts networkFiles/accounts/accounts.json

# 2. Confirm the real proof actually verifies before trusting anything else
python3 verify_real_proof.py \
  --deployed-contracts deployed_contracts.json \
  --proof-json fixtures/proof.json \
  --public-json fixtures/public.json

# 3. Edit the config
nano config.yml

# 4. Run the sweep
python3 zkroam_workload.py --config config.yml
```

Output lands in `results/zkroam/`: `<experiment>_sweep_summary.csv` (one row
per `nproofs`/run), `detail/` (per-transaction CSVs), and
`<experiment>_resource_usage.csv` (host CPU/mem, if monitoring is enabled).

---

## Key concept: `nproofs` vs `num_vmnos`

These are two independent dials — conflating them was a real bug in an
earlier version of this script, worth being deliberate about:

- **`nproofs`** (sweep value, e.g. 8/64/128/…) — how many CDR proofs *one*
  VMNO settlement bundles together. This is what the Rust binary swept and
  what `offchain.logs_dir` has logs for.
- **`num_vmnos`** (config value) — how many such VMNO settlements are
  actually submitted on-chain *this run*. The real workload size, unrelated
  to `nproofs`.

They combine as:

```
individual leg -> nproofs * num_vmnos total transactions   (each VMNO posts nproofs individual proof-verify txs)
aggregate  leg -> num_vmnos total transactions              (each VMNO posts exactly 1 attestation)
```

---

## Why not a real on-chain SnarkPack verifier?

`SnarkPackAggregateAnchor.sol` is an attestation contract, not a
verifier. Verification of the aggregate proof
(`snarkpack::verify_aggregate_proof`, ~12-14ms, already measured by the
Rust binary) happens **off-chain**, and an authorized verifier attests to
the result on-chain.

This isn't a shortcut taken for convenience — the actual blocker is
structural: SnarkPack's GIPA/TIPP recursion requires the verifier to do
generic target-field (GT/Fq12) exponentiation and multiplication across
`log2(nproofs)` rounds, and **no EVM chain has a precompile for that on
any curve** — `ecPairing` (0x08) only ever answers "does this product of
pairings equal 1?", it never exposes a raw GT value you can exponentiate.
Moving the circuit to BN254 fixed the *individual*-proof verifier (which
only needs the standard 4-pairing Groth16 check, which the precompile
does support) — it does not fix this. A real aggregate verifier means
implementing audited Fq12 tower-field arithmetic in Solidity from
scratch, which is its own project, not an add-on to a benchmark harness.

`keccak_transcript.rs` solves a *different*, smaller piece of that
puzzle (Fiat-Shamir challenge compatibility) in case that project is
ever undertaken — it is not itself a verifier.

### Attestation model: `anchorAggregateProof` vs `relayAggregateProof`

Two ways to record an attestation:

- **`anchorAggregateProof(settlementId, commitment, nproofs)`** —
  `onlyVerifier`. `msg.sender` *is* the attesting identity, so it must be
  both authorized and pay its own transaction's nonce. Simple, but every
  attestation from one verifier account competes for Besu's ~200
  in-flight nonce-gap budget.
- **`relayAggregateProof(settlementId, commitment, nproofs, signers[],
  signatures[])`** — permissionless. Verifier identity comes from an
  EIP-712 signature (`ecrecover`), not `msg.sender`, so a verifier signs
  off-chain for free and *any* account can relay it on-chain. This is
  what `zkroam_workload.py`'s aggregate leg actually uses:
  `contracts.verifier_key_index` picks who signs, submission round-robins
  the *entire* account pool with no special authorization needed.

---

## zkRoam workload

The zkRoam driver reads all settings from YAML. Its only command-line option is `--config`:


### YAML configuration

The main sections in `config.yml` are:

```yaml
experiment:
  name: zkroam_real_proof_smoketest

network:
  topology: networkFiles/topology.json
  accounts: accounts/accounts.json

sweep:
  nproofs: [8]
  runs: 1
  mode: both                 # individual, aggregate, or both

workload:
  num_vmnos: 500

offchain:
  logs_dir: ../aggregation/aggregation_bn254/output

contracts:
  verifier_address: null
  deployed_contracts: deployed_contracts.json
  proof_json: fixtures/proof.json
  public_json: fixtures/public.json
  verifier_key_index: 0

execution:
  rate: 500.0
  workers: 32
  receipt_workers: 16
  receipt_timeout: 120.0
  poll_interval: 0.25

monitoring:
  enabled: true
  interval: 1.0

output:
  out_dir: results/zkroam
```

### Configuration guidance

- Start with `nproofs: [8]`, `runs: 1`, and a small `num_vmnos` such as `1` or `5`.
- Use `mode: individual` to validate the real CDR verifier before testing the aggregate leg.
- Use `mode: aggregate` only after the aggregate anchor is deployed and its calldata path is configured.
- Set `runs` to the number of repetitions needed for your measurements.
- Set `nproofs` to values for which off-chain JSON logs exist.
- Keep `rate`, `workers`, and `receipt_workers` low during a smoke test, then increase them for the experiment.
- `monitoring.enabled` records host resource usage in the output directory.

### Real proof setup

The individual leg can call a deployed `CDRVerifier` contract. For a true positive verification, both proof files must match the deployed verification key and circuit:

```yaml
contracts:
  proof_json: fixtures/proof.json
  public_json: fixtures/public.json
```

In this checkout the available fixture names are `fixtures/proof.json` and `fixtures/public.json`.

The Circom fixture circuit and the Arkworks aggregation circuit are not automatically interchangeable. Their Poseidon inputs and public-signal orders differ. A proof generated by the Rust/SnarkPack circuit should not be assumed to verify against the Circom-generated `CDRVerifier`.

### Deploy the verifier contracts

To authorize more verifier identities for the aggregate anchor:

```bash
python3 workload/deploy_verifiers.py \
    --verifier-pool-size 3 \
    --out deployed_contracts.json
```

The verifier pool is for authorized signing identities; transaction submission throughput is controlled separately by the account pool and worker settings.

### Recommended experiment sequence

1. Start the QBFT network from `net-heterogeneity/04b-run-baseline` or `net-heterogeneity/04-run-network`.
2. Confirm `networkFiles/topology.json` and `accounts/accounts.json` exist and that accounts are funded.
3. Build the off-chain aggregation logs for the proof counts you want to sweep.
4. Deploy the verifier contracts to write `deployed_contracts.json`.
5. Run `verify_real_proof.py` with one known-good proof.
6. Copy `config.yml` to a separate experiment config and set `num_vmnos` to a small value.
7. Run `mode: individual` once and inspect the output.
8. Run `mode: both` for the comparison experiment.
9. Save the complete `results/zkroam/` directory and the exact YAML config used.
10. Stop the network after the run with the network project's Docker command.


### Output files

The zkRoam driver writes a summary CSV and per-leg detail files under `output.out_dir`, normally `results/zkroam/`:

- Summary rows identify `nproofs_per_vmno`, `num_vmnos`, and `run`.
- Individual and aggregate legs record submitted, confirmed, failed, and timed-out transactions.
- Gas fields distinguish estimated gas from observed `gasUsed` when a deployed contract was called.
- Pipeline latency combines off-chain proof/aggregation measurements with on-chain confirmation latency.
- When both legs have comparable values, `gas_savings_pct` and `pipeline_speedup_x` are included.
- Resource monitoring writes `<experiment-name>_resource_usage.csv`.
- Per-transaction details are stored in `results/zkroam/detail/`.


---

## Troubleshooting / reading the numbers correctly

**`gas/tx` lands exactly on a suspiciously round number (150000, 200000,
400000).** That's the hardcoded fallback used when `estimate_gas` threw
an exception — not a real measurement. Check the printed
`estimate_gas failed (...)` line for why (commonly: replay guard tripped
by a reused `settlementId`, or an unauthorized signer).

**Aggregate leg gas/tx was pinned at fallback, or only 1 of N confirmed
with real `gasUsed`.** Almost certainly a reused `settlementId` — the
replay guard is keyed on `(settlementId, verifier)`, so `num_vmnos` calls
attesting the *same* settlement will all revert after the first. Each
VMNO's settlement needs its own distinct id; this is already how the
current script builds calldata, but if you're modifying it, don't
share one id across a batch.

**`Transaction nonce is too distant from current sender nonce.`** You're
forcing too many transactions through too few sender accounts. If you're
using the direct `anchorAggregateProof` path, this happens fast (~200 tx
per single verifier account) — switch to `relayAggregateProof`
(the default here), which decouples verifier identity from the
submitting account entirely.

**Real throughput is way below `execution.rate`, but blocks show ~0%
gas utilization.** Gas is not your bottleneck. Check per-block
`tx count / block period` from the node's logs — if it's stable
regardless of pending-pool depth, you're wall-clock-bound by actual EVM
execution time (e.g. a real BN254 pairing check costs real CPU time,
independent of its gas price). Isolate this by running the same leg
against a plain non-contract address with identical calldata size (no
execution at all) and comparing throughput — the gap is your real
per-tx compute cost, not something `execution.rate` can fix.

**Resource-usage CSV has empty metric columns.** `psutil` isn't installed
— see Prerequisites. Check for the `NOTE` row `write_csv()` appends when
this happens; if it's not there, the columns being empty is a real
observation (host truly idle), not this bug.

**Resource-usage CSV shows a long block of `idle`/`setup:*` phases before
anything interesting.** That's the one-time RPC-heavy setup (connecting,
`load_accounts`, `load_nonces` — one call per account) tagged separately
now (`setup:connecting`/`setup:loading_accounts`/`setup:loading_nonces`)
specifically so it doesn't get lumped into a generic `idle` blob.
`load_nonces` fetches all accounts' nonces concurrently, so this should
be seconds even with hundreds of accounts, not the linear-in-account-count
wait it used to be — if it's still slow, check RPC endpoint latency
directly rather than assuming the monitor is broken.

**`estimate_gas` succeeds but the deployed contract returns unexpected
results.** For the individual leg, double check `public.json`'s signal
order matches what the *specific* circuit was compiled with — the circom
circuit in `fixtures/CDRCircuit.circom` uses a different signal order
(`[T, hashCDR, r_mb, r_sms, r_voice]`) and a different Poseidon arity (3
inputs) than the arkworks circuit in `snarkpack_aggregation/src/
constraints.rs` (`[r_sms, r_mb, r_voice, t, hash_cdr]`, 5 inputs). These
are two different relations — a proof from one can never verify against
a verifier built from the other. `verify_real_proof.py` exists precisely
to catch this class of mismatch before you run a full sweep on top of it.

---

## Reproducibility checklist

For every reported result, preserve:

- The exact YAML configuration.
- The git commit of this repository.
- The topology and account-generation configuration.
- The Besu image version.
- The off-chain aggregation JSON logs.
- The Python and dependency versions.
- The complete `results/zkroam/` output directory.
- The number of repetitions and any discarded or failed runs.
