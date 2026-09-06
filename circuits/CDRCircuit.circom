pragma circom 2.0.0;

// Matches aggregation/aggregation_bn254/src/constraints.rs.
// This is the per-CDR circuit whose Groth16 proofs are aggregated by
// SnarkPack off-chain. 

// The Rust benchmark uses a deliberately non-production Poseidon instance:
// width = 3, rate = 2, capacity = 1, alpha = 5,
// full rounds = 8, partial rounds = 57.
template RustPoseidonPermutation() {
    signal input in[3];
    signal output out[3];

    signal state[66][3];
    signal ark[65][3];
    signal sbox[65][3];
    signal square[65][3];
    signal fourth[65][3];

    for (var i = 0; i < 3; i++) {
        state[0][i] <== in[i];
    }

    for (var round = 0; round < 65; round++) {
        for (var i = 0; i < 3; i++) {
            // Rust's poseidon_params_example() uses ark[i][j] = i + j + 1.
            ark[round][i] <== state[round][i] + round + i + 1;
        }

        // First 4 and last 4 rounds are full rounds. The middle 57
        // rounds apply the S-box only to state[0].
        if (round < 4 || round >= 61) {
            for (var i = 0; i < 3; i++) {
                square[round][i] <== ark[round][i] * ark[round][i];
                fourth[round][i] <== square[round][i] * square[round][i];
                sbox[round][i] <== fourth[round][i] * ark[round][i];
            }
        } else {
            square[round][0] <== ark[round][0] * ark[round][0];
            fourth[round][0] <== square[round][0] * square[round][0];
            sbox[round][0] <== fourth[round][0] * ark[round][0];
            for (var i = 1; i < 3; i++) {
                sbox[round][i] <== ark[round][i];
            }
        }

        // Rust MDS matrix:
        // [1 2 3]
        // [3 1 2]
        // [2 3 1]
        state[round + 1][0] <== sbox[round][0] + 2 * sbox[round][1] + 3 * sbox[round][2];
        state[round + 1][1] <== 3 * sbox[round][0] + sbox[round][1] + 2 * sbox[round][2];
        state[round + 1][2] <== 2 * sbox[round][0] + 3 * sbox[round][1] + sbox[round][2];
    }

    for (var i = 0; i < 3; i++) {
        out[i] <== state[65][i];
    }
}

template CDRCircuit() {
    // Private witness values.
    signal input n_sms;
    signal input n_mb;
    signal input n_min;
    signal input randomness;
    signal input session_id;

    // Public signals. This order matches new_input() in constraints.rs.
    signal input r_sms;
    signal input r_mb;
    signal input r_voice;
    signal input t;
    signal input hash_cdr;

    signal sms_total;
    signal mb_total;
    signal voice_total;
    signal computed_total;

    sms_total <== n_sms * r_sms;
    mb_total <== n_mb * r_mb;
    voice_total <== n_min * r_voice;
    computed_total <== sms_total + mb_total + voice_total;
    computed_total === t;

    // ark-sponge PoseidonSponge absorbs two field elements per permutation.
    // The state layout is [capacity, rate_0, rate_1].
    signal input_state_0[3];
    signal permutation_state_0[3];
    signal input_state_1[3];
    signal permutation_state_1[3];
    signal input_state_2[3];
    signal poseidon_state[3];

    input_state_0[0] <== 0;
    input_state_0[1] <== n_sms;
    input_state_0[2] <== n_mb;

    component permutation_0 = RustPoseidonPermutation();
    for (var i = 0; i < 3; i++) {
        permutation_0.in[i] <== input_state_0[i];
    }
    for (var i = 0; i < 3; i++) {
        permutation_state_0[i] <== permutation_0.out[i];
    }

    input_state_1[0] <== permutation_state_0[0];
    input_state_1[1] <== permutation_state_0[1] + n_min;
    input_state_1[2] <== permutation_state_0[2] + randomness;

    component permutation_1 = RustPoseidonPermutation();
    for (var i = 0; i < 3; i++) {
        permutation_1.in[i] <== input_state_1[i];
    }
    for (var i = 0; i < 3; i++) {
        permutation_state_1[i] <== permutation_1.out[i];
    }

    input_state_2[0] <== permutation_state_1[0];
    input_state_2[1] <== permutation_state_1[1] + session_id;
    input_state_2[2] <== permutation_state_1[2];

    component permutation_2 = RustPoseidonPermutation();
    for (var i = 0; i < 3; i++) {
        permutation_2.in[i] <== input_state_2[i];
    }
    for (var i = 0; i < 3; i++) {
        poseidon_state[i] <== permutation_2.out[i];
    }

    poseidon_state[1] === hash_cdr;
}

component main {public [r_sms, r_mb, r_voice, t, hash_cdr]} = CDRCircuit();
