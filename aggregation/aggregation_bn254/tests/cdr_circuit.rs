//! Integration tests for the CDR circuit + Groth16 + SnarkPack
//! aggregation pipeline, on BN254.
//!
//! This replaces the old tests/aggregation.rs, which referenced a
//! `Benchmark` circuit type that doesn't exist anywhere in this project
//! (src/constraints.rs only defines `CDRCircuit`) and declared
//! `mod constraints;` with no matching tests/constraints.rs - each file
//! under tests/ is its own crate root, so that module path can't resolve
//! to src/constraints.rs. It looks like unadapted boilerplate copied
//! from the snarkpack crate's own test suite and never wired up to this
//! project; it could not have compiled. These tests use the project's
//! real CDRCircuit via the new src/lib.rs instead.
//!
//! Run with: cargo test

use ark_bn254::{Bn254, Fr};
use ark_groth16::{prepare_verifying_key, Groth16, PreparedVerifyingKey, Proof};
use ark_relations::r1cs::{ConstraintSynthesizer, ConstraintSystem, ConstraintSystemRef};
use ark_sponge::poseidon::PoseidonSponge;
use ark_sponge::CryptographicSponge;

use rand_chacha::ChaChaRng;
use rand_core::SeedableRng;

use snarkpack::srs::{ProverSRS, VerifierSRS};
use snarkpack::Transcript;

use aggregation_bn254::constraints::CDRCircuit;
use aggregation_bn254::poseidon_params_example;

// ============================================================
// Shared fixtures
// ============================================================

/// A self-consistent CDRCircuit: `t` and `hash_cdr` are derived the same
/// way src/main.rs derives them, so the circuit is satisfiable by
/// construction. `CDRCircuit` isn't `Clone` (its Poseidon config isn't
/// meant to be cheaply shared across owned circuit instances the way
/// Groth16's by-value APIs need), so tests call this fresh each time
/// they need a circuit instance - exactly what src/main.rs already does
/// in its proof-generation loop.
fn valid_circuit() -> CDRCircuit {
    let poseidon_params = poseidon_params_example();

    let r_sms = Fr::from(2);
    let r_mb = Fr::from(3);
    let r_voice = Fr::from(5);

    let n_sms = Fr::from(10);
    let n_mb = Fr::from(20);
    let n_min = Fr::from(30);

    let randomness = Fr::from(7);
    let session_id = Fr::from(42);

    let t = n_sms * r_sms + n_mb * r_mb + n_min * r_voice;

    let mut sponge = PoseidonSponge::new(&poseidon_params);
    sponge.absorb(&n_sms);
    sponge.absorb(&n_mb);
    sponge.absorb(&n_min);
    sponge.absorb(&randomness);
    sponge.absorb(&session_id);
    let hash_cdr = sponge.squeeze_field_elements(1)[0];

    CDRCircuit {
        n_sms,
        n_mb,
        n_min,
        randomness,
        session_id,
        r_sms,
        r_mb,
        r_voice,
        t,
        hash_cdr,
        poseidon_params,
    }
}

/// Public inputs in the exact order src/main.rs uses:
/// [r_sms, r_mb, r_voice, t, hash_cdr] - must match constraints.rs's
/// new_input() call order or verification will check the wrong values
/// against the wrong wires.
fn public_inputs(c: &CDRCircuit) -> Vec<Fr> {
    vec![c.r_sms, c.r_mb, c.r_voice, c.t, c.hash_cdr]
}

type SetupOutput = (
    PreparedVerifyingKey<Bn254>,
    ProverSRS<Bn254>,
    VerifierSRS<Bn254>,
    Vec<Proof<Bn254>>,
    Vec<Vec<Fr>>,
);

/// Full off-chain setup for the aggregation tests: Groth16 params for the
/// CDR circuit, a fake (non-ceremony) SnarkPack SRS specialized to
/// `nproofs`, and `nproofs` freshly generated proofs over the same
/// public inputs (mirrors src/main.rs's benchmark loop, minus the
/// timing/memory instrumentation).
///
/// `nproofs` MUST be a power of two - snarkpack::srs::GenericSRS::specialize()
/// asserts this internally.
fn aggregate_setup(nproofs: usize, rng: &mut ChaChaRng) -> SetupOutput {
    let params =
        Groth16::<Bn254>::generate_random_parameters_with_reduction(valid_circuit(), rng)
            .unwrap();
    let pvk = prepare_verifying_key(&params.vk);

    let srs = snarkpack::srs::setup_fake_srs::<Bn254, _>(rng, nproofs);
    let (prover_srs, ver_srs) = srs.specialize(nproofs);

    let inputs = public_inputs(&valid_circuit());
    let all_inputs = vec![inputs; nproofs];

    let proofs: Vec<_> = (0..nproofs)
        .map(|_| {
            Groth16::<Bn254>::create_random_proof_with_reduction(valid_circuit(), &params, rng)
                .expect("proof creation failed")
        })
        .collect();

    (pvk, prover_srs, ver_srs, proofs, all_inputs)
}

// ============================================================
// Circuit-level tests (no proving system involved - just the R1CS)
// ============================================================

#[test]
fn circuit_is_satisfied_with_correct_witness() {
    let cs: ConstraintSystemRef<Fr> = ConstraintSystem::new_ref();
    valid_circuit().generate_constraints(cs.clone()).unwrap();

    assert!(
        cs.is_satisfied().unwrap(),
        "a correctly-constructed witness must satisfy every constraint"
    );
    assert!(cs.num_constraints() > 0, "circuit should not be empty");
}

#[test]
fn circuit_rejects_wrong_usage_total() {
    let mut c = valid_circuit();
    // t != n_sms*r_sms + n_mb*r_mb + n_min*r_voice anymore
    c.t += Fr::from(1);

    let cs: ConstraintSystemRef<Fr> = ConstraintSystem::new_ref();
    c.generate_constraints(cs.clone()).unwrap();

    assert!(
        !cs.is_satisfied().unwrap(),
        "a tampered usage total `t` must violate the enforce_equal constraint"
    );
}

#[test]
fn circuit_rejects_wrong_hash() {
    let mut c = valid_circuit();
    c.hash_cdr += Fr::from(1);

    let cs: ConstraintSystemRef<Fr> = ConstraintSystem::new_ref();
    c.generate_constraints(cs.clone()).unwrap();

    assert!(
        !cs.is_satisfied().unwrap(),
        "a tampered hash_cdr must violate the Poseidon-hash constraint"
    );
}

// ============================================================
// Groth16 (single proof) round-trip on BN254
// ============================================================

#[test]
fn groth16_proof_roundtrip_on_bn254() {
    let mut rng = ChaChaRng::seed_from_u64(1);

    let params =
        Groth16::<Bn254>::generate_random_parameters_with_reduction(valid_circuit(), &mut rng)
            .unwrap();
    let pvk = prepare_verifying_key(&params.vk);

    let inputs = public_inputs(&valid_circuit());
    let proof = Groth16::<Bn254>::create_random_proof_with_reduction(
        valid_circuit(),
        &params,
        &mut rng,
    )
    .expect("proof creation failed");

    let ok = Groth16::<Bn254>::verify_proof(&pvk, &proof, &inputs).unwrap();
    assert!(ok, "a genuine proof over its own public inputs must verify");
}

#[test]
fn groth16_proof_rejects_tampered_public_input() {
    let mut rng = ChaChaRng::seed_from_u64(2);

    let params =
        Groth16::<Bn254>::generate_random_parameters_with_reduction(valid_circuit(), &mut rng)
            .unwrap();
    let pvk = prepare_verifying_key(&params.vk);

    let mut inputs = public_inputs(&valid_circuit());
    let proof = Groth16::<Bn254>::create_random_proof_with_reduction(
        valid_circuit(),
        &params,
        &mut rng,
    )
    .expect("proof creation failed");

    // Claim a different settlement total `t` than what was actually proven.
    inputs[3] += Fr::from(1);

    let ok = Groth16::<Bn254>::verify_proof(&pvk, &proof, &inputs).unwrap();
    assert!(!ok, "a proof must not verify against public inputs it wasn't generated for");
}

// ============================================================
// SnarkPack aggregation round-trip on BN254
// ============================================================

#[test]
fn snarkpack_aggregate_roundtrip_on_bn254() {
    let mut rng = ChaChaRng::seed_from_u64(3);
    let nproofs = 4; // power of two, required by srs.specialize()

    let (pvk, prover_srs, ver_srs, proofs, all_inputs) = aggregate_setup(nproofs, &mut rng);

    let mut prover_transcript = snarkpack::transcript::new_merlin_transcript(b"cdr-aggregation-test");
    prover_transcript.append(b"inputs", &all_inputs);

    let agg_proof = snarkpack::aggregate_proofs(&prover_srs, &mut prover_transcript, &proofs)
        .expect("aggregation should succeed over valid proofs");

    let mut verifier_transcript =
        snarkpack::transcript::new_merlin_transcript(b"cdr-aggregation-test");
    verifier_transcript.append(b"inputs", &all_inputs);

    snarkpack::verify_aggregate_proof(
        &ver_srs,
        &pvk,
        &all_inputs,
        &agg_proof,
        &mut rng,
        &mut verifier_transcript,
    )
    .expect("a genuine aggregate proof over its own public inputs must verify");
}

#[test]
fn snarkpack_aggregate_rejects_tampered_public_inputs() {
    let mut rng = ChaChaRng::seed_from_u64(4);
    let nproofs = 4;

    let (pvk, prover_srs, ver_srs, proofs, mut all_inputs) = aggregate_setup(nproofs, &mut rng);

    let mut prover_transcript = snarkpack::transcript::new_merlin_transcript(b"cdr-aggregation-test");
    prover_transcript.append(b"inputs", &all_inputs);

    let agg_proof = snarkpack::aggregate_proofs(&prover_srs, &mut prover_transcript, &proofs)
        .expect("aggregation should succeed over valid proofs");

    // Claim a different `t` for one of the aggregated proofs after the
    // fact - the verifier's recomputed linear combination should no
    // longer match what's baked into the aggregate proof.
    all_inputs[0][3] += Fr::from(1);

    let mut verifier_transcript =
        snarkpack::transcript::new_merlin_transcript(b"cdr-aggregation-test");
    verifier_transcript.append(b"inputs", &all_inputs);

    let result = snarkpack::verify_aggregate_proof(
        &ver_srs,
        &pvk,
        &all_inputs,
        &agg_proof,
        &mut rng,
        &mut verifier_transcript,
    );

    assert!(
        result.is_err(),
        "aggregate verification must fail when a public input was tampered with post-aggregation"
    );
}

#[test]
fn snarkpack_aggregate_rejects_mismatched_transcript() {
    // A verifier who doesn't bind the same data into the Fiat-Shamir
    // transcript the prover used (wrong label, or forgetting to append
    // the public inputs) re-derives different challenges than the ones
    // the aggregate proof was actually built against. This is what
    // cryptographically binds an aggregate proof to one specific
    // settlement batch, not merely "the pairing checks happen to pass" -
    // so it's worth testing on its own, separately from tampering with
    // the inputs themselves.
    let mut rng = ChaChaRng::seed_from_u64(5);
    let nproofs = 4;

    let (pvk, prover_srs, ver_srs, proofs, all_inputs) = aggregate_setup(nproofs, &mut rng);

    let mut prover_transcript = snarkpack::transcript::new_merlin_transcript(b"cdr-aggregation-test");
    prover_transcript.append(b"inputs", &all_inputs);

    let agg_proof = snarkpack::aggregate_proofs(&prover_srs, &mut prover_transcript, &proofs)
        .expect("aggregation should succeed over valid proofs");

    // Different domain-separation label => different Fiat-Shamir challenges.
    let mut verifier_transcript = snarkpack::transcript::new_merlin_transcript(b"wrong-label");
    verifier_transcript.append(b"inputs", &all_inputs);

    let result = snarkpack::verify_aggregate_proof(
        &ver_srs,
        &pvk,
        &all_inputs,
        &agg_proof,
        &mut rng,
        &mut verifier_transcript,
    );

    assert!(
        result.is_err(),
        "aggregate verification must fail when the verifier's transcript doesn't match the prover's"
    );
}

#[test]
fn snarkpack_aggregate_roundtrip_at_larger_nproofs() {
    // Same round-trip at a size closer to your actual sweep
    // (output/experiment_log_8_*.json etc.), still small enough to run
    // fast as part of `cargo test`.
    let mut rng = ChaChaRng::seed_from_u64(6);
    let nproofs = 8;

    let (pvk, prover_srs, ver_srs, proofs, all_inputs) = aggregate_setup(nproofs, &mut rng);

    let mut prover_transcript = snarkpack::transcript::new_merlin_transcript(b"cdr-aggregation-test-8");
    prover_transcript.append(b"inputs", &all_inputs);

    let agg_proof = snarkpack::aggregate_proofs(&prover_srs, &mut prover_transcript, &proofs)
        .expect("aggregation should succeed over valid proofs");

    let mut verifier_transcript =
        snarkpack::transcript::new_merlin_transcript(b"cdr-aggregation-test-8");
    verifier_transcript.append(b"inputs", &all_inputs);

    snarkpack::verify_aggregate_proof(
        &ver_srs,
        &pvk,
        &all_inputs,
        &agg_proof,
        &mut rng,
        &mut verifier_transcript,
    )
    .expect("a genuine 8-proof aggregate must verify");
}
