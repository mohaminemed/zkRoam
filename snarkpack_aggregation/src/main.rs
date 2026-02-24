use ark_bls12_381::{Bls12_381, Fr};
use ark_groth16::{prepare_verifying_key, Groth16};
use ark_sponge::poseidon::{PoseidonConfig, PoseidonSponge};
use ark_sponge::CryptographicSponge;
use rand_core::SeedableRng;
use std::time::{Instant};
use std::fs::File;
use serde::{Serialize, Deserialize};

mod constraints;
use crate::constraints::CDRCircuit;
use ark_relations::r1cs::{ConstraintSystem, ConstraintSystemRef, ConstraintSynthesizer};
use snarkpack;
use snarkpack::Transcript;

#[derive(Serialize, Deserialize)]
struct ProofLog {
    proof_index: usize,
    creation_time_ms: f64,
}

#[derive(Serialize, Deserialize)]
struct ExperimentLog {
    nproofs: usize,
    parameter_gen_time_ms: f64,
    proofs: Vec<ProofLog>,
    single_proof_verify_ms: f64,
    aggregation_prove_ms: f64,
    aggregation_verify_ms: f64,
}

fn poseidon_params_example() -> PoseidonConfig<Fr> {
    let full_rounds = 8;
    let partial_rounds = 57;
    let alpha = 5;       // exponent used in the S-box
    let rate = 2;        
    let capacity = 1;    

    let mds: Vec<Vec<Fr>> = vec![
        vec![Fr::from(1u64), Fr::from(2u64), Fr::from(3u64)],
        vec![Fr::from(3u64), Fr::from(1u64), Fr::from(2u64)],
        vec![Fr::from(2u64), Fr::from(3u64), Fr::from(1u64)]
    ];
    let ark: Vec<Vec<Fr>> = (0..(full_rounds + partial_rounds))
        .map(|i| vec![Fr::from(i as u64 + 1), Fr::from(i as u64 + 2), Fr::from(i as u64 + 3)])
        .collect();

    PoseidonConfig::<Fr> {
        full_rounds,
        partial_rounds,
        alpha,
        ark,
        mds,
        rate,
        capacity,
    }
}

fn main() {
    let run= 3;
    let nproofs = 512;
    let mut rng = rand_chacha::ChaChaRng::seed_from_u64(1u64);

    // Example inputs
    let r_sms = Fr::from(2u64);
    let r_mb = Fr::from(3u64);
    let r_voice = Fr::from(5u64);
    let n_sms = Fr::from(10u64);
    let n_mb = Fr::from(20u64);
    let n_min = Fr::from(30u64);
    let randomness = Fr::from(7u64);
    let session_id = Fr::from(42u64);

    // compute t
    let t = n_sms * r_sms + n_mb * r_mb + n_min * r_voice;

    // Poseidon parameters
    let poseidon_params = poseidon_params_example();

    // compute hashCDR using Poseidon sponge
    let mut sponge = PoseidonSponge::<Fr>::new(&poseidon_params);
    sponge.absorb(&n_sms);
    sponge.absorb(&n_mb);
    sponge.absorb(&n_min);
    sponge.absorb(&randomness);
    sponge.absorb(&session_id);
    let hash_cdr = sponge.squeeze_field_elements(1)[0];

    println!("Public t: {:?}, hashCDR: {:?}", t, hash_cdr);

    // ========== 1. PARAMETER GENERATION ==========
     let cs: ConstraintSystemRef<Fr> = ConstraintSystem::new_ref();

// Create a separate circuit only for counting constraints
let count_circuit = CDRCircuit {
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
    poseidon_params: poseidon_params.clone(),
};

count_circuit.generate_constraints(cs.clone()).unwrap();
println!("CDRCircuit total constraints: {}", cs.num_constraints());

    let start_params = Instant::now();
    let circuit = CDRCircuit {
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
        poseidon_params: poseidon_params.clone(),
    };


    let params = Groth16::<Bls12_381>::generate_random_parameters_with_reduction(circuit, &mut rng)
        .expect("parameter generation failed");
    let param_time = start_params.elapsed().as_secs_f64() * 1000.0;
    println!("Parameter generation time: {:?} ms", param_time);

    let pvk = prepare_verifying_key(&params.vk);

    // ========== 2. Setup SRS ==========
    let srs = snarkpack::srs::setup_fake_srs::<Bls12_381, _>(&mut rng, nproofs);
    let (prover_srs, ver_srs) = srs.specialize(nproofs);

    // ========== 3. Create proofs ==========
    let inputs: Vec<Fr> = vec![r_sms, r_mb, r_voice, t, hash_cdr];
    let all_inputs = (0..nproofs).map(|_| inputs.clone()).collect::<Vec<_>>();

    let mut proofs = Vec::new();
    let mut proof_logs = Vec::new();

    for i in 0..nproofs {
        let circuit = CDRCircuit {
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
            poseidon_params: poseidon_params.clone(),
        };
    
       
        let start_single = Instant::now();
        let proof = Groth16::<Bls12_381>::create_random_proof_with_reduction(circuit, &params, &mut rng)
            .expect("proof creation failed");
        let elapsed_ms = start_single.elapsed().as_secs_f64() * 1000.0;
        println!("Proof {} creation time: {:?} ms", i, elapsed_ms);

        proof_logs.push(ProofLog {
            proof_index: i,
            creation_time_ms: elapsed_ms,
        });

        proofs.push(proof);
    }

    // ========== 4. Single proof verification ==========
    let start_verify = Instant::now();
    let r = Groth16::<Bls12_381>::verify_proof(&pvk, &proofs[0], &inputs).unwrap();
    assert!(r);
    let verify_time = start_verify.elapsed().as_secs_f64() * 1000.0;
    println!("Single proof verification time: {:?} ms", verify_time);

    // ========== 5. Aggregation proving ==========
    let mut prover_transcript = snarkpack::transcript::new_merlin_transcript(b"test aggregation");
    prover_transcript.append(b"public-inputs", &all_inputs);

    let start_agg = Instant::now();
    let aggregate_proof = snarkpack::aggregate_proofs(&prover_srs, &mut prover_transcript, &proofs)
        .expect("aggregation failed");
    let agg_prove_time = start_agg.elapsed().as_secs_f64() * 1000.0;
    println!("Aggregation proving time: {:?} ms", agg_prove_time);

    // ========== 6. Aggregation verification ==========
    let mut ver_transcript = snarkpack::transcript::new_merlin_transcript(b"test aggregation");
    ver_transcript.append(b"public-inputs", &all_inputs);

    let start_agg_verify = Instant::now();
    snarkpack::verify_aggregate_proof(
        &ver_srs,
        &pvk,
        &all_inputs,
        &aggregate_proof,
        &mut rng,
        &mut ver_transcript,
    )
    .expect("aggregation verification failed");
    let agg_verify_time = start_agg_verify.elapsed().as_secs_f64() * 1000.0;
    println!("Aggregation verification time: {:?} ms", agg_verify_time);

    // ========== 7. Write results to CSV & JSON ==========
    let experiment_log = ExperimentLog {
        nproofs,
        parameter_gen_time_ms: param_time,
        proofs: proof_logs,
        single_proof_verify_ms: verify_time,
        aggregation_prove_ms: agg_prove_time,
        aggregation_verify_ms: agg_verify_time,
    };

    let json_file = File::create(format!("experiment_log_{}_{}.json", nproofs, run)).unwrap();
    serde_json::to_writer_pretty(json_file, &experiment_log).unwrap();

   
    println!("Results written to experiment_log_{}_{}.json", nproofs, run);

    
}
