pragma circom 2.0.0;

include "./poseidon.circom";

template CDRCircuit() {
    // CDR details
    //  - n_sms: the number of SMS messages
    //  - n_mb: the total MB of bandwidth consumed
    //  - n_min: the total number of minutes made in a voice call
    signal input n_sms;
    signal input n_mb;
    signal input n_min;

    // Public Inputs: 
    //  - T: the total charge computed
    //  - hashCDR: the hash commitment of the CDR (provided by the UE)
    //  - r_sms: charge rate per SMS
    //  - r_mb: charge rate per MB bandwidth
    //  - r_voice: charge rate per minute of foice call
    signal input T;
    signal input hashCDR;
    signal input r_sms;
    signal input r_mb;
    signal input r_voice ;


    signal smsTotal;
    signal mbTotal;
    signal voiceTotal;
    signal computedTotal;

    smsTotal <== n_sms * r_sms;
    mbTotal <== n_mb * r_mb;
    voiceTotal <== n_min * r_voice;

    computedTotal <== smsTotal + mbTotal + voiceTotal;
    computedTotal === T;

    // Compute the Poseidon hash of the CDR data.
    component poseidonHash = Poseidon(3);
    poseidonHash.inputs[0] <== n_sms;
    poseidonHash.inputs[1] <== n_mb;
    poseidonHash.inputs[2] <== n_min;
    
    // Enforce that the computed hash equals the public hashCDR.
    poseidonHash.out === hashCDR;
}

component main {public [T, hashCDR, r_mb, r_sms, r_voice]} = CDRCircuit();