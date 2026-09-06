use ark_bls12_381::{Bls12_381, Fr};
use ark_groth16::{prepare_verifying_key, Groth16};
use ark_sponge::poseidon::{PoseidonConfig, PoseidonSponge};
use ark_sponge::CryptographicSponge;
use ark_relations::r1cs::{ConstraintSystem, ConstraintSystemRef, ConstraintSynthesizer};

use rand_core::SeedableRng;
use rand_chacha::ChaChaRng;

use std::time::Instant;
use std::fs::File;
use std::env;

use serde::{Serialize, Deserialize};

use sysinfo::{System, ProcessesToUpdate};

use snarkpack;
use snarkpack::Transcript;

mod constraints;
use crate::constraints::CDRCircuit;


// ================= MEMORY STRUCT =================

#[derive(Serialize, Deserialize, Clone)]
struct MemoryLog {
    rss_bytes: u64,
    virtual_bytes: u64,
}


// ================= PROOF STRUCT =================

#[derive(Serialize, Deserialize)]
struct ProofLog {
    index: usize,
    time_ms: f64,
    memory: MemoryLog,
}


// ================= EXP STRUCT =================

#[derive(Serialize, Deserialize)]
struct ExperimentLog {

    nproofs: usize,
    constraints: usize,

    param_time_ms: f64,
    param_memory: MemoryLog,

    proofs: Vec<ProofLog>,

    verify_time_ms: f64,

    aggregation_time_ms: f64,
    aggregation_memory: MemoryLog,

    aggregation_verify_time_ms: f64,
    aggregation_verify_memory: MemoryLog,

    peak_memory_bytes: u64,
}


// ================= MEMORY FUNCTION =================

fn get_memory(sys: &mut System) -> MemoryLog {

    let pid = sysinfo::get_current_pid().unwrap();

    sys.refresh_processes(
        ProcessesToUpdate::Some(&[pid]),
        true,
    );

    let process = sys.process(pid).unwrap();

    MemoryLog {
        rss_bytes: process.memory(),
        virtual_bytes: process.virtual_memory(),
    }
}


// ================= POSEIDON PARAMS =================

fn poseidon_params_example() -> PoseidonConfig<Fr> {

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


// ================= MAIN =================

fn main() {

    let args: Vec<String> = env::args().collect();

    let run: usize = args
     .get(1)
     .expect("Missing run parameter")
     .parse()
     .expect("run must be integer");

    let nproofs: usize = args
      .get(2)
      .expect("Missing nproofs parameter")
      .parse()
      .expect("nproofs must be integer");

    let mut sys = System::new_all();
    let mut peak_memory = 0;

    let mut rng = ChaChaRng::seed_from_u64(1);


    // ================= INPUT =================

    let r_sms = Fr::from(2);
    let r_mb = Fr::from(3);
    let r_voice = Fr::from(5);

    let n_sms = Fr::from(10);
    let n_mb = Fr::from(20);
    let n_min = Fr::from(30);

    let randomness = Fr::from(7);
    let session_id = Fr::from(42);

    let t = n_sms * r_sms + n_mb * r_mb + n_min * r_voice;

    let poseidon_params = poseidon_params_example();

    let mut sponge = PoseidonSponge::new(&poseidon_params);

    sponge.absorb(&n_sms);
    sponge.absorb(&n_mb);
    sponge.absorb(&n_min);
    sponge.absorb(&randomness);
    sponge.absorb(&session_id);

    let hash_cdr = sponge.squeeze_field_elements(1)[0];


    // ================= CONSTRAINT COUNT =================

    let cs: ConstraintSystemRef<Fr> = ConstraintSystem::new_ref();

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

    let constraints = cs.num_constraints();

    println!("Constraints: {}", constraints);


    // ================= PARAMETER GENERATION =================

    let start = Instant::now();

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

    let params =
        Groth16::<Bls12_381>::generate_random_parameters_with_reduction(
            circuit,
            &mut rng,
        ).unwrap();

    let param_time = start.elapsed().as_secs_f64() * 1000.0;

    let param_memory = get_memory(&mut sys);

    peak_memory = peak_memory.max(param_memory.rss_bytes);

    println!(
        "Parameter Memory: {} MB",
        param_memory.rss_bytes / 1024 / 1024
    );

    let pvk = prepare_verifying_key(&params.vk);


    // ================= SRS =================

    let srs =
        snarkpack::srs::setup_fake_srs::<Bls12_381,_>(&mut rng, nproofs);

    let (prover_srs, ver_srs) =
        srs.specialize(nproofs);


    // ================= CREATE PROOFS =================

    let inputs =
        vec![r_sms, r_mb, r_voice, t, hash_cdr];

    let all_inputs =
        vec![inputs.clone(); nproofs];

    let mut proofs = vec![];
    let mut proof_logs = vec![];

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

        let start = Instant::now();

        let proof =
            Groth16::<Bls12_381>::create_random_proof_with_reduction(
                circuit,
                &params,
                &mut rng,
            ).expect("proof creation failed");

        let time =
            start.elapsed().as_secs_f64() * 1000.0;

        let mem =
            get_memory(&mut sys);

        peak_memory =
            peak_memory.max(mem.rss_bytes);

        println!(
            "Proof {}: {} ms, {} MB",
            i,
            time,
            mem.rss_bytes / 1024 / 1024
        );

        proofs.push(proof);

        proof_logs.push(
            ProofLog {
                index: i,
                time_ms: time,
                memory: mem,
            }
        );
    }


    // ================= SINGLE VERIFY =================

    let start = Instant::now();

    let r =
        Groth16::<Bls12_381>::verify_proof(
            &pvk,
            &proofs[0],
            &inputs,
        ).unwrap();

    assert!(r);

    let verify_time =
        start.elapsed().as_secs_f64() * 1000.0;


    // ================= AGGREGATION =================

    let mut transcript =
        snarkpack::transcript::new_merlin_transcript(b"test");

    transcript.append(b"inputs", &all_inputs);

    let start = Instant::now();

    let agg_proof =
        snarkpack::aggregate_proofs(
            &prover_srs,
            &mut transcript,
            &proofs,
        ).unwrap();

    let agg_time =
        start.elapsed().as_secs_f64() * 1000.0;

    let agg_mem =
        get_memory(&mut sys);

    peak_memory =
        peak_memory.max(agg_mem.rss_bytes);


    // ================= AGG VERIFY =================

    let mut transcript =
        snarkpack::transcript::new_merlin_transcript(b"test");

    transcript.append(b"inputs", &all_inputs);

    let start = Instant::now();

    snarkpack::verify_aggregate_proof(
        &ver_srs,
        &pvk,
        &all_inputs,
        &agg_proof,
        &mut rng,
        &mut transcript,
    ).unwrap();

    let agg_verify_time =
        start.elapsed().as_secs_f64() * 1000.0;

    let agg_verify_mem =
        get_memory(&mut sys);

    peak_memory =
        peak_memory.max(agg_verify_mem.rss_bytes);


    // ================= SAVE JSON =================

    let log = ExperimentLog {

        nproofs,
        constraints,

        param_time_ms: param_time,
        param_memory,

        proofs: proof_logs,

        verify_time_ms: verify_time,

        aggregation_time_ms: agg_time,
        aggregation_memory: agg_mem,

        aggregation_verify_time_ms: agg_verify_time,
        aggregation_verify_memory: agg_verify_mem,

        peak_memory_bytes: peak_memory,
    };

    let file =
        File::create(
            format!(
                "output/experiment_log_{}_{}.json",
                nproofs,
                run,
            )
        ).unwrap();

    serde_json::to_writer_pretty(
        file,
        &log,
    ).unwrap();


    println!(
        "\nPeak Memory: {} MB",
        peak_memory / 1024 / 1024
    );
}