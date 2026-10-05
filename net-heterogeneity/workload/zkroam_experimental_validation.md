# TODO: zkRoam Experimental Validation 

## Objective

This document defines the experimental validation required to address the two remaining evaluation points:

1. **Non-ideal network conditions:** evaluate zkRoam beyond a local network using WAN emulation with latency, jitter, and packet loss (e.g., `tc netem`).
2. **Multi-VMNO/HMNO concurrent deployment:** evaluate concurrent VMNO/HMNO activity rather than only scaling the number of proofs in a single pair, to provide evidence for larger-scale roaming environments.


---

## 1. Experimental Workflow

For every experimental configuration, use the following three-step procedure:

```text
1. Run network
       ↓
2. Deploy contracts
       ↓
3. Run workload
```

# 2. Experimental Dimensions

The evaluation consists of two main scaling experiments, each executed under both baseline and WAN QBFT configurations.

## Experiment 1 — Proof-count scaling

### Objective

Measure how zkRoam behaves as the number of proofs increases while keeping the number of VMNOs fixed.

### Configuration

Fix:

```text
nVMNO = 64
```

Sweep the number of proofs over:

```text
8, 16, 32, 64, 128, 256, 512, 1024
```

For zkRoam's aggregation mechanism, we distinguish clearly between:

- number of generated individual proofs (per session);
- number of aggregated proofs (per vmno);

This is important because increasing `nbProofs` does not necessarily imply a proportional increase in on-chain verification work when proof aggregation is used.

---

# 3. Experiment 2 — VMNO Scaling

## Objective

Evaluate a concurrent multi-VMNO/HMNO deployment rather than only increasing the number of proofs handled by a single VMNO/HMNO pair.

This experiment directly addresses the requirement to evaluate **large-scale roaming environments**.

## Configuration

Fix:

```text
nbProofs = 64
```

Sweep the number of VMNOs over:

```text
8, 16, 32, 64, 128, 256, 512, 1024
```

The VMNOs must represent **concurrent participants**, rather than simply multiplying the number of proofs submitted by one VMNO.

The workload therefore distribute requests across the configured VMNOs/HMNOs.

Conceptually:

```text
VMNO 1  ──┐
VMNO 2  ──┤
VMNO 3  ──┤
...       ├──> QBFT network ──> zkRoam contracts
VMNO N  ──┘
```

# Commands to run: 

```bash
  cd net-heterogeneity 
  # first run a smoke test across the 3 networks
  python3 workload/run-experiment.py --network wan baseline heterogeneous --num-vmnos 8 --nproofs 8
  # exp 1: full sweep on nbproofs
  python3 workload/run-experiment.py --network wan baseline heterogeneous --num-vmnos 64 
  # exp 2: full sweep on num-vmnos
  python3 workload/run-experiment.py --network wan baseline heterogeneous --num-vmnos 128 256 512 1024 2048 --nproofs 64
```
