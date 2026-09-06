//! keccak_transcript.rs
//!
//! A `snarkpack::Transcript` implementation using Keccak256 instead of
//! Merlin/STROBE. This is the actual prerequisite for a real on-chain
//! SnarkPack verifier: Merlin's STROBE construction has no reasonable
//! Solidity port, whereas keccak256 is a single EVM opcode (0x20) with
//! well-defined byte-for-byte semantics, so a Solidity contract can
//! derive IDENTICAL Fiat-Shamir challenges to this by construction,
//! rather than by attempting a bit-exact reimplementation of STROBE.
//!
//! This does NOT solve the GT (Fq12) arithmetic problem described in
//! chat - that's a separate, larger blocker. This only solves "the
//! verifier and prover must derive the same challenge scalars," which
//! is a precondition for a real verifier, not sufficient on its own.
//!
//! Checked against the real reference implementation
//! (snarkpack::transcript::{Transcript, new_merlin_transcript}) before
//! writing this, not written from memory of what Merlin "probably" does:
//!   - `domain_sep()` is defined on the trait but never called by
//!     aggregate_proofs()/verify_aggregate_proof() or by the example
//!     main.rs - it's dead code from this pipeline's point of view.
//!     Implemented here anyway to satisfy the trait; don't go looking
//!     for why it "isn't firing," it never runs in this pipeline.
//!   - Merlin's own challenge_scalar() retry loop re-derives a full
//!     fresh 64-byte value from STROBE's *already-advanced* internal
//!     state on each retry (STROBE ratchets on every squeeze, so the
//!     same label produces different bytes the second time called on
//!     the same transcript). This implementation achieves the same
//!     "must-differ-on-retry" property by an explicit counter byte
//!     absorbed into the hash chain instead, since a plain keccak256
//!     hash has no implicit ratcheting the way a sponge-based
//!     construction does - different mechanism, same required property.
//!
//! `Transcript` is a plain trait in the snarkpack crate
//! (fn domain_sep / fn append / fn challenge_scalar), and
//! aggregate_proofs()/verify_aggregate_proof() are generic over any
//! T: Transcript - so this is a drop-in replacement for
//! `new_merlin_transcript`, no changes needed elsewhere in your
//! aggregation code.
//!
//! NOT compiled/run here - no Rust toolchain in the sandbox this was
//! written in. 

use ark_ff::fields::Field;
use ark_serialize::{CanonicalSerialize, Compress};
use sha3::{Digest, Keccak256};

pub struct Keccak256Transcript {
    state: Vec<u8>,
}

impl Keccak256Transcript {
    pub fn new(label: &'static [u8]) -> Self {
        let mut t = Self { state: Vec::new() };
        t.absorb(b"init", label);
        t
    }

    /// Mirrors what a Solidity contract would do with
    /// `keccak256(abi.encodePacked(state, label, len(data), data))`:
    /// re-hash the running state together with a label and new bytes,
    /// and use the digest as the new state. This keeps every prior
    /// append() bound into every later challenge, same purpose as
    /// Merlin's rolling STROBE state, but with primitives Solidity can
    /// match exactly instead of approximate.
    fn absorb(&mut self, label: &'static [u8], data: &[u8]) {
        let mut hasher = Keccak256::new();
        hasher.update(&self.state);
        hasher.update(label);
        // Explicit length prefix: without it, append(b"a", &[1,2]) then
        // append(b"bc", &[3]) would hash identically to append(b"ab",
        // &[1,2,3]) under naive concatenation - the same ambiguity
        // abi.encodePacked has, so a Solidity port must length-prefix
        // the same way for the transcripts to actually match.
        hasher.update((data.len() as u64).to_be_bytes());
        hasher.update(data);
        self.state = hasher.finalize().to_vec();
    }
}

impl snarkpack::Transcript for Keccak256Transcript {
    fn domain_sep(&mut self) {
        // See module doc: unused by aggregate_proofs()/verify_aggregate_proof()
        // in this pipeline, implemented only to satisfy the trait.
        self.absorb(b"dom-sep", b"groth16-aggregation-snarkpack-keccak");
    }

    fn append<S: CanonicalSerialize>(&mut self, label: &'static [u8], element: &S) {
        // CanonicalSerialize compressed encoding: for G1/G2 points this is
        // the standard compressed-point byte format; for TargetField (GT)
        // elements it's the Fq12 coordinate encoding. A Solidity contract
        // reconstructing this transcript needs to encode field elements
        // the SAME way (big-endian uint256 words per coordinate is the
        // natural EVM choice) - this is exactly the kind of byte-format
        // detail that must be nailed down and cross-tested before trusting
        // any on-chain verifier built on top of this, not assumed.
        let mut buf = vec![0u8; element.serialized_size(Compress::Yes)];
        element
            .serialize_compressed(&mut buf[..])
            .expect("serialization failed");
        self.absorb(label, &buf);
    }

    fn challenge_scalar<F: Field>(&mut self, label: &'static [u8]) -> F {
        // Same rejection-sampling structure as the Merlin impl: derive a
        // 64-byte (double-width, for uniformity modulo the scalar field)
        // value, try to interpret as a field element with a valid
        // inverse, retry with an advancing counter on failure.
        let mut counter: u8 = 0;
        loop {
            self.absorb(label, &[counter]);

            let mut buf = [0u8; 64];
            buf[..32].copy_from_slice(&self.state);

            let mut hasher2 = Keccak256::new();
            hasher2.update(&self.state);
            hasher2.update(b"challenge-ext");
            buf[32..].copy_from_slice(&hasher2.finalize());

            if let Some(e) = F::from_random_bytes(&buf) {
                if e.inverse().is_some() {
                    return e;
                }
            }
            counter = counter.wrapping_add(1);
        }
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use ark_bn254::{Fr, G1Projective};
    use ark_ec::Group;
    use snarkpack::Transcript;

    /// Mirrors snarkpack's own transcript test (src/transcript.rs) for
    /// the Merlin implementation: same label + same appended data must
    /// always produce the same challenge, on separate transcript
    /// instances. This is the minimum property required for a prover
    /// and a verifier (or, eventually, a Solidity contract) to derive
    /// matching Fiat-Shamir challenges independently.
    #[test]
    fn deterministic_challenge() {
        let mut transcript = Keccak256Transcript::new(b"test");
        transcript.append(b"point", &G1Projective::generator());
        let f1 = transcript.challenge_scalar::<Fr>(b"scalar");

        let mut transcript2 = Keccak256Transcript::new(b"test");
        transcript2.append(b"point", &G1Projective::generator());
        let f2 = transcript2.challenge_scalar::<Fr>(b"scalar");

        assert_eq!(f1, f2);
    }

    /// A different label must (with overwhelming probability) produce a
    /// different challenge - catches an accidental label/data mixup in
    /// absorb()'s length-prefixing.
    #[test]
    fn different_label_different_challenge() {
        let mut t1 = Keccak256Transcript::new(b"test");
        t1.append(b"point", &G1Projective::generator());
        let f1 = t1.challenge_scalar::<Fr>(b"scalar-a");

        let mut t2 = Keccak256Transcript::new(b"test");
        t2.append(b"point", &G1Projective::generator());
        let f2 = t2.challenge_scalar::<Fr>(b"scalar-b");

        assert_ne!(f1, f2);
    }
}