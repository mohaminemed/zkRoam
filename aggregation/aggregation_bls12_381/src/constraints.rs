use ark_bls12_381::Fr;
use ark_r1cs_std::{prelude::*, fields::fp::FpVar};
use ark_relations::r1cs::{ConstraintSynthesizer, ConstraintSystemRef, SynthesisError};
use ark_sponge::poseidon::{PoseidonConfig};
use ark_sponge::constraints::CryptographicSpongeVar;
use ark_sponge::poseidon::constraints::PoseidonSpongeVar;

pub struct CDRCircuit {
    // Private inputs
    pub n_sms: Fr,
    pub n_mb: Fr,
    pub n_min: Fr,
    pub randomness: Fr,
    pub session_id: Fr,

    // Public inputs
    pub t: Fr,
    pub hash_cdr: Fr,
    pub r_sms: Fr,
    pub r_mb: Fr,
    pub r_voice: Fr,

 
    // Poseidon parameters (use PoseidonConfig in 0.4.0)
    pub poseidon_params: PoseidonConfig<Fr>,

}

impl ConstraintSynthesizer<Fr> for CDRCircuit {
    fn generate_constraints(self, cs: ConstraintSystemRef<Fr>) -> Result<(), SynthesisError> {
        // --- witness variables ---
        let n_sms_var = FpVar::new_witness(cs.clone(), || Ok(self.n_sms))?;
        let n_mb_var = FpVar::new_witness(cs.clone(), || Ok(self.n_mb))?;
        let n_min_var = FpVar::new_witness(cs.clone(), || Ok(self.n_min))?;
        let randomness_var = FpVar::new_witness(cs.clone(), || Ok(self.randomness))?;
        let session_var = FpVar::new_witness(cs.clone(), || Ok(self.session_id))?;

        // --- public inputs ---
        let r_sms_var = FpVar::new_input(cs.clone(), || Ok(self.r_sms))?;
        let r_mb_var = FpVar::new_input(cs.clone(), || Ok(self.r_mb))?;
        let r_voice_var = FpVar::new_input(cs.clone(), || Ok(self.r_voice))?;
        let t_var = FpVar::new_input(cs.clone(), || Ok(self.t))?;
        let hash_cdr_var = FpVar::new_input(cs.clone(), || Ok(self.hash_cdr))?;

        // --- compute totals ---
        let sms_total_var = &n_sms_var * &r_sms_var;
        let mb_total_var = &n_mb_var * &r_mb_var;
        let voice_total_var = &n_min_var * &r_voice_var;

        // --- sum totals ---
        let computed_total_var = &sms_total_var + &mb_total_var + &voice_total_var;

        // enforce computedTotal == t
        computed_total_var.enforce_equal(&t_var)?;

        // --- Poseidon hash gadget ---
        let mut poseidon_sponge = PoseidonSpongeVar::<Fr>::new(cs.clone(), &self.poseidon_params);

        poseidon_sponge.absorb(&n_sms_var)?;
        poseidon_sponge.absorb(&n_mb_var)?;
        poseidon_sponge.absorb(&n_min_var)?;
        poseidon_sponge.absorb(&randomness_var)?;
        poseidon_sponge.absorb(&session_var)?;

        let poseidon_out = poseidon_sponge.squeeze_field_elements(1)?;
        poseidon_out[0].enforce_equal(&hash_cdr_var)?;

        Ok(())
    }
}