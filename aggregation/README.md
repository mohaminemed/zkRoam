
## File map

| File | What it is |
|---|---|
| `run_experiments.sh` | Drives the Rust off-chain sweep (`output/experiment_log_<n>_<run>.json`). |
| `bn254/`, `bls12_381` | Rust Arkwork circuits + SnarkPack aggregation for BN254 and BLS12-381. |
| `keccak_transcript.rs` | Draft Keccak-based Fiat-Shamir transcript (Merlin replacement) — prerequisite for any future EVM-compatible verifier, not sufficient on its own (see caveats). |


- **`keccak_transcript.rs`** is an untested draft (no Rust toolchain was
  available to compile/verify it here) — treat it as a starting point,
  not a merged fix.