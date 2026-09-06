# Workload Experiments

This directory contains the Python workload drivers used by the Besu QBFT network experiments in `net-heterogeneity/`. There are two related workloads:

- `besu_benchmark.py`: a generic rate-controlled transaction producer and receipt poller.
- `zkroam_snarkpack_workload.py`: a zkRoam-specific sweep comparing individual Groth16 verification transactions with one aggregate-anchor transaction per VMNO.

The scripts submit transactions to the RPC endpoints listed in `networkFiles/topology.json`, use accounts from `accounts/accounts.json`, and write measurements under `results/`.

## Important: run from the network directory

The bundled configuration uses paths relative to `net-heterogeneity/`, not relative to this `workload/` directory. Start every command from the network directory:

```bash
cd zkRoam/net-heterogeneity
```

When using the zkRoam driver, pass the configuration explicitly:

```bash
python3 workload/zkroam_snarkpack_workload.py --config workload/config.yml
```


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

## Input files

| File | Description |
| --- | --- |
| `config.yml` | Real-proof workload configuration. It enables both workload legs and several runs at `nproofs: 8, ..., 2048`. |
| `config.dummy.yml` | Safe template configuration with no deployed verifier or proof files configured. |
| `besu_benchmark.py` | Generic transaction benchmark implementation. |
| `zkroam_snarkpack_workload.py` | YAML-driven zkRoam workload and sweep driver. |
| `deploy_verifiers.py` | Compiles and deploys the individual verifier and aggregate anchor contracts. |
| `verify_real_proof.py` | Small proof-verification smoke test. |

The workload expects:

- `networkFiles/topology.json` with validator RPC host ports.
- `accounts/accounts.json` with funded accounts and private keys.
- Off-chain aggregation logs such as `aggregation/aggregation_bn254/output/experiment_log_8_1.json`.
- Optional Solidity deployment output in `deployed_contracts.json`.
- Optional Circom proof inputs in `fixtures/proof.json` and `fixtures/public.json`.

## Generic Besu benchmark

Use `besu_benchmark.py` when you want to measure transaction submission and confirmation behavior without the zkRoam-specific sweep:

```bash
python3 workload/besu_benchmark.py \
    --topology networkFiles/topology.json \
    --accounts accounts/accounts.json \
    --num-tx 1000 \
    --rate 500 \
    --workers 48 \
    --receipt-workers 32 \
    --out results/details_1000tx_500.csv \
    --summary-out results/summary_1000tx_500.csv \
    --resource-out results/resources_1000tx_500.csv
```

Useful options:

| Option | Meaning | Default |
| --- | --- | --- |
| `--num-tx` | Number of transactions to submit. | `500` |
| `--rate` | Target submission rate in transactions per second. | `100.0` |
| `--workers` | Concurrent transaction submission workers. | `32` |
| `--receipt-workers` | Concurrent receipt polling workers. | `16` |
| `--receipt-timeout` | Maximum wait per receipt, in seconds. | `120` |
| `--poll-interval` | Receipt polling interval, in seconds. | `0.25` |
| `--out` | Per-transaction CSV output. | `results/benchmark.csv` |
| `--summary-out` | Aggregate summary CSV output. | `results/summary.csv` |
| `--resource-out` | Host resource time-series CSV. | `results/resource_usage.csv` |
| `--no-monitor-resources` | Disable CPU and memory sampling. | Monitoring enabled |

The summary contains submission throughput, confirmed throughput, success rate, confirmation latency percentiles, and failure/timeout counts. The detailed CSV contains one row per submitted transaction, including its RPC endpoint and receipt status.

## zkRoam SnarkPack workload

The zkRoam driver reads all settings from YAML. Its only command-line option is `--config`:

```bash
python3 workload/zkroam_snarkpack_workload.py \
    --config workload/config.yml
```

The driver sweeps every value in `sweep.nproofs` and every run from `1` through `sweep.runs`. For each sweep point it can execute two legs:

### Individual leg

Each VMNO submits `nproofs` full proof-verification transactions. The total number of transactions is:

```text
nproofs * workload.num_vmnos
```

### Aggregate leg

Each VMNO submits one aggregate-anchor transaction. The total number of transactions is:

```text
workload.num_vmnos
```

These are different size controls:

- `sweep.nproofs` is the number of CDR proofs in one VMNO settlement.
- `workload.num_vmnos` is the number of VMNO settlements submitted in the run.

Do not use `num_vmnos` as a replacement for the proof-count sweep. Increasing `nproofs` increases the individual leg's transaction count, while increasing `num_vmnos` increases the number of settlements in both legs.

## YAML configuration

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

## Real proof setup

The individual leg can call a deployed `CDRVerifier` contract. For a true positive verification, both proof files must match the deployed verification key and circuit:

```yaml
contracts:
  proof_json: fixtures/proof.json
  public_json: fixtures/public.json
```

In this checkout the available fixture names are `fixtures/proof.json` and `fixtures/public.json`.

The Circom fixture circuit and the Arkworks aggregation circuit are not automatically interchangeable. Their Poseidon inputs and public-signal orders differ. A proof generated by the Rust/SnarkPack circuit should not be assumed to verify against the Circom-generated `CDRVerifier`.

## Deploy the verifier contracts

Deploy both contracts against the running Besu network:

```bash
python3 workload/deploy_verifiers.py \
    --topology networkFiles/topology.json \
    --accounts accounts/accounts.json \
    --contracts-dir contracts \
    --out deployed_contracts.json
```

This deploys:

- `CDRVerifier.sol`, unless `--skip-cdr-verifier` is supplied.
- `SnarkPackAggregateAnchor.sol`.

The first account in `accounts/accounts.json` is the deployer. The output file contains the chain ID, deployed addresses, and ABIs. The workload reads it through `contracts.deployed_contracts`.

To authorize more verifier identities for the aggregate anchor:

```bash
python3 workload/deploy_verifiers.py \
    --verifier-pool-size 3 \
    --out deployed_contracts.json
```

The verifier pool is for authorized signing identities; transaction submission throughput is controlled separately by the account pool and worker settings.

## Verify one real proof first

Before launching a large sweep, run the standalone sanity check:

```bash
python3 workload/verify_real_proof.py \
    --topology networkFiles/topology.json \
    --accounts accounts/accounts.json \
    --proof-json fixtures/proof.json \
    --public-json fixtures/public.json \
    --deployed-contracts deployed_contracts.json
```

This performs an `eth_call` to `verifyProof()` and exits successfully only when the proof is accepted. To also submit a real transaction and report `gasUsed`, add:

```bash
--send-tx
```

If the check fails, verify the circuit, verification key, proof file, public-signal order, chain, and deployed address before running the full workload.

## Recommended experiment sequence

1. Start the QBFT network from `net-heterogeneity/04b-run-baseline` or `net-heterogeneity/04-run-network`.
2. Confirm `networkFiles/topology.json` and `accounts/accounts.json` exist and that accounts are funded.
3. Build the off-chain aggregation logs for the proof counts you want to sweep.
4. Deploy the verifier contracts and write `deployed_contracts.json`.
5. Run `verify_real_proof.py` with one known-good proof.
6. Copy `config.yml` to a separate experiment config and set `num_vmnos` to a small value.
7. Run `mode: individual` once and inspect the output.
8. Run `mode: both` for the comparison experiment.
9. Save the complete `results/zkroam/` directory and the exact YAML config used.
10. Stop the network after the run with the network project's Docker command.

Example smoke-test configuration override:

```bash
cp workload/config.yml workload/config.smoke.yml
```

Edit `config.smoke.yml` to use `nproofs: [8]`, `runs: 1`, `num_vmnos: 1`, and `mode: individual`, then run:

```bash
python3 workload/zkroam_snarkpack_workload.py \
    --config workload/config.smoke.yml
```

## Output files

The zkRoam driver writes a summary CSV and per-leg detail files under `output.out_dir`, normally `results/zkroam/`:

- Summary rows identify `nproofs_per_vmno`, `num_vmnos`, and `run`.
- Individual and aggregate legs record submitted, confirmed, failed, and timed-out transactions.
- Gas fields distinguish estimated gas from observed `gasUsed` when a deployed contract was called.
- Pipeline latency combines off-chain proof/aggregation measurements with on-chain confirmation latency.
- When both legs have comparable values, `gas_savings_pct` and `pipeline_speedup_x` are included.
- Resource monitoring writes `<experiment-name>_resource_usage.csv`.
- Per-transaction details are stored in `results/zkroam/detail/`.

Inspect a result directory with:

```bash
find results/zkroam -maxdepth 2 -type f -print
```

## Troubleshooting

### `FileNotFoundError` for topology, accounts, or logs
Run from `net-heterogeneity/` and pass `--config workload/config.yml`. Check that the paths in the YAML match the current checkout.

### No off-chain log for a sweep point
For `nproofs: [256]` and `runs: 3`, the driver expects files such as:

```text
aggregation/aggregation_bn254/output/experiment_log_256_1.json
aggregation/aggregation_bn254/output/experiment_log_256_2.json
aggregation/aggregation_bn254/output/experiment_log_256_3.json
```

Generate the missing Rust aggregation runs or remove that proof count from the sweep.

### No deployed verifier found
Run `deploy_verifiers.py` first and ensure `contracts.deployed_contracts` points to the resulting JSON file. Alternatively, set `contracts.verifier_address` for the individual verifier address.

### The proof does not verify
Use `verify_real_proof.py` before the full workload. The proof and public-input files must match the deployed verifier's circuit and verification key. A valid proof from a different circuit is still invalid for this verifier.

### Transactions time out or revert
Check that the Besu network is producing blocks, the sending accounts are funded, the RPC ports in the topology are reachable, and `receipt_timeout` is long enough for the configured network delay. Reduce `rate`, `workers`, and `num_vmnos` while diagnosing.

### Real contract measurements
If `deployed_contracts.json` does not contain `cdr_verifier` or `aggregate_anchor`, the driver falls back to raw calldata-cost estimates. Treat those results as transport/calldata measurements, not EVM verifier gas measurements.

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
