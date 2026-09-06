//! Library target for snarkpack_aggregation.
//!
//! Splitting this out of src/main.rs is what makes tests/ able to reuse
//! the real CDRCircuit and Poseidon parameters instead of redefining
//! (or, as in the old tests/aggregation.rs, failing to define) them.
//! `cargo build` / `cargo run` still produce the same benchmark binary
//! (src/main.rs), now backed by this lib; `cargo test` builds this lib
//! plus every file under tests/ as a separate integration-test binary.

pub mod constraints;

use ark_bn254::Fr;
use ark_sponge::poseidon::PoseidonConfig;

/// Demo Poseidon parameters shared by the CLI binary and the test suite.
///
/// NOT secure parameters (the `ark`/`mds` values are small sequential
/// placeholders, not values from a real Poseidon parameter generation
/// process) - fine for constraint-count/benchmarking/testing purposes,
/// not for anything meant to resist real preimage/collision attacks.
pub fn poseidon_params_example() -> PoseidonConfig<Fr> {
    let full_rounds = 8;
    let partial_rounds = 57;

    let alpha = 5;
    let rate = 2;
    let capacity = 1;

    let mds = vec![
        vec![Fr::from(1), Fr::from(2), Fr::from(3)],
        vec![Fr::from(3), Fr::from(1), Fr::from(2)],
        vec![Fr::from(2), Fr::from(3), Fr::from(1)],
    ];

    let ark = (0..(full_rounds + partial_rounds))
        .map(|i| {
            vec![
                Fr::from(i as u64 + 1),
                Fr::from(i as u64 + 2),
                Fr::from(i as u64 + 3),
            ]
        })
        .collect();

    PoseidonConfig {
        full_rounds,
        partial_rounds,
        alpha,
        ark,
        mds,
        rate,
        capacity,
    }
}
