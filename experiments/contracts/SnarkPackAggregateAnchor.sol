// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/*
    SnarkPackAggregateAnchor
    -------------------------------------------------------------------
    Design note (read this before benchmarking gas numbers against it):

    A full on-chain re-verification of a SnarkPack aggregate proof means
    executing the TIPP/GIPA recursive check: O(log nproofs) rounds of
    target-field (GT/Fq12) exponentiation and multiplication, which no
    EVM precompile exposes on any curve - see the chat note on this for
    why that's a standalone cryptographic-engineering project, not an
    add-on to this benchmark.

    So this contract implements the pattern real systems use today:

      Verification of the aggregate proof (snarkpack::verify_aggregate_proof,
      the ~12-14ms call the Rust binary already measures) happens OFF-chain,
      by one or more authorized verifiers. Each verifier attests to a
      commitment - keccak256(aggregate_proof_bytes || public_inputs ||
      nproofs) - either directly (anchorAggregateProof, onlyVerifier) or
      via an EIP-712 signature relayed by ANY account (relayAggregateProof).
      Once `threshold` authorized verifiers agree on the same commitment
      for a given settlement id, it's considered anchored/final.

      The relay path exists specifically so verifier IDENTITY (who is
      trusted to attest) is decoupled from transaction-submitting
      NONCE (whose account pays gas and gets rate-limited by Besu's
      ~200 in-flight nonce-gap cap). A verifier signs off-chain for
      free; submission load spreads across ordinary, unprivileged
      accounts instead of requiring a pool of specially-authorized ones.
    -------------------------------------------------------------------
*/

contract SnarkPackAggregateAnchor {

    bytes32 private constant ATTEST_TYPEHASH = keccak256(
        "AggregateAttestation(bytes32 settlementId,bytes32 commitment,uint64 nproofs)"
    );
    bytes32 private immutable DOMAIN_SEPARATOR;

    // secp256k1n / 2 - the standard signature-malleability bound (same
    // constant OpenZeppelin's ECDSA.sol uses). Any valid (r,s,v) has a
    // mathematically valid twin (r, n-s, v^1) recovering to the SAME
    // signer; rejecting s-values above this bound picks one canonical
    // representative per signature so a malleable twin can't be used
    // to front-run/replay an observed valid attestation.
    uint256 private constant SECP256K1N_HALF =
        0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;

    // Bounds relayAggregateProof's loop so a relayer can't accidentally
    // (or maliciously, at their own expense) build a batch that can
    // never fit in a block and always reverts mid-loop.
    uint256 private constant MAX_BATCH_SIZE = 50;

    struct Attestation {
        bytes32 commitment;   // keccak256(aggProofBytes || publicInputs || nproofs)
        uint64  nproofs;
        uint64  timestamp;
    }

    address public owner;
    uint256 public threshold = 1;

    mapping(address => bool) public isVerifier;
    uint256 public verifierCount;

    // settlementId => verifier => attestation
    mapping(bytes32 => mapping(address => Attestation)) public attestations;
    // settlementId => count of verifiers agreeing on the finalized commitment
    mapping(bytes32 => uint256) public agreementCount;
    mapping(bytes32 => bytes32) public finalizedCommitment;
    mapping(bytes32 => bool) public isFinalized;

    event Anchored(
        bytes32 indexed settlementId,
        address indexed verifier,
        bytes32 commitment,
        uint64 nproofs
    );
    event Finalized(bytes32 indexed settlementId, bytes32 commitment, uint64 nproofs);

    error InvalidSignature();

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    modifier onlyVerifier() {
        require(isVerifier[msg.sender], "not an authorized verifier");
        _;
    }

    constructor(address initialVerifier) {
        require(initialVerifier != address(0), "zero verifier");
        owner = msg.sender;
        isVerifier[initialVerifier] = true;
        verifierCount = 1;
        DOMAIN_SEPARATOR = keccak256(abi.encode(
            keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
            keccak256(bytes("SnarkPackAggregateAnchor")),
            keccak256(bytes("1")),
            block.chainid,
            address(this)
        ));
    }

    function addVerifier(address v) external onlyOwner {
        require(!isVerifier[v], "already a verifier");
        isVerifier[v] = true;
        verifierCount += 1;
    }

    function setThreshold(uint256 t) external onlyOwner {
        require(t > 0 && t <= verifierCount, "bad threshold");
        threshold = t;
    }

    /// @notice Anchor a commitment to an aggregate proof you have already
    /// verified off-chain (snarkpack::verify_aggregate_proof returned Ok).
    /// Direct path: msg.sender IS the attesting verifier, so it must be
    /// authorized AND pay/nonce for its own transaction. Fine for a
    /// verifier that's happy to submit its own txs; for spreading
    /// submission load across unprivileged accounts, use
    /// relayAggregateProof instead.
    /// @param settlementId caller-chosen id for this settlement batch
    ///        (e.g. keccak256(sessionId, epoch)).
    /// @param commitment keccak256(aggProofBytes || publicInputsBytes || nproofs)
    function anchorAggregateProof(
        bytes32 settlementId,
        bytes32 commitment,
        uint64 nproofs
    ) external onlyVerifier {

        _recordAttestation(settlementId, commitment, nproofs, msg.sender);
    }

    /// @notice Submit attestations signed by authorized off-chain verifiers.
    /// @dev Deliberately has NO onlyVerifier/onlyOwner restriction: the
    /// caller is only a relayer paying gas and does not need to be
    /// authorized itself. Authorization is enforced per-signer via
    /// ecrecover + isVerifier below, not via msg.sender. This is what
    /// lets an arbitrary pool of ordinary accounts submit on behalf of
    /// a small set of trusted verifier keys without those keys ever
    /// needing their own transaction nonce budget.
    function relayAggregateProof(
        bytes32 settlementId,
        bytes32 commitment,
        uint64 nproofs,
        address[] calldata signers,
        bytes[] calldata signatures
    ) external {
        require(signers.length == signatures.length, "length mismatch");
        require(signers.length > 0, "no attestations");
        require(signers.length <= MAX_BATCH_SIZE, "batch too large");

        bytes32 digest = _attestationDigest(settlementId, commitment, nproofs);
        address previousSigner;
        for (uint256 i = 0; i < signers.length; ++i) {
            address signer = signers[i];
            require(signer > previousSigner, "signers not sorted");
            previousSigner = signer;
            require(isVerifier[signer], "unauthorized signer");
            if (_recover(digest, signatures[i]) != signer) {
                revert InvalidSignature();
            }
            // "already attested" is enforced inside _recordAttestation -
            // no need to check it again here first.
            _recordAttestation(settlementId, commitment, nproofs, signer);
        }
    }

    /// @notice Canonical commitment used by off-chain verifiers and relayers.
    /// The bytes must be exactly those verified by snarkpack off-chain.
    function aggregateCommitment(
        bytes calldata aggregateProof,
        bytes calldata publicInputs,
        uint64 nproofs
    ) external pure returns (bytes32) {
        return keccak256(abi.encodePacked(aggregateProof, publicInputs, nproofs));
    }

    function _recordAttestation(
        bytes32 settlementId,
        bytes32 commitment,
        uint64 nproofs,
        address verifier
    ) internal {

        require(attestations[settlementId][verifier].timestamp == 0, "already attested");

        attestations[settlementId][verifier] = Attestation({
            commitment: commitment,
            nproofs: nproofs,
            timestamp: uint64(block.timestamp)
        });

        emit Anchored(settlementId, verifier, commitment, nproofs);

        if (!isFinalized[settlementId]) {

            if (finalizedCommitment[settlementId] == bytes32(0)) {
                finalizedCommitment[settlementId] = commitment;
            }

            if (finalizedCommitment[settlementId] == commitment) {
                agreementCount[settlementId] += 1;

                if (agreementCount[settlementId] >= threshold) {
                    isFinalized[settlementId] = true;
                    emit Finalized(settlementId, commitment, nproofs);
                }
            }
            // Disagreement handling (different verifiers submitting
            // different commitments for the same settlementId) is left
            // to off-chain dispute resolution / governance - out of
            // scope for a benchmark harness.
        }
    }

    function _attestationDigest(
        bytes32 settlementId,
        bytes32 commitment,
        uint64 nproofs
    ) internal view returns (bytes32) {
        return keccak256(abi.encodePacked(
            "\x19\x01",
            DOMAIN_SEPARATOR,
            keccak256(abi.encode(ATTEST_TYPEHASH, settlementId, commitment, nproofs))
        ));
    }

    function _recover(bytes32 digest, bytes calldata signature)
        internal
        pure
        returns (address signer)
    {
        if (signature.length != 65) revert InvalidSignature();
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset, 32))
            v := byte(0, calldataload(add(signature.offset, 64)))
        }
        if (uint256(s) > SECP256K1N_HALF) revert InvalidSignature();
        if (v < 27) v += 27;
        if (v != 27 && v != 28) revert InvalidSignature();
        signer = ecrecover(digest, v, r, s);
        if (signer == address(0)) revert InvalidSignature();
    }

    function getAttestation(bytes32 settlementId, address verifier)
        external
        view
        returns (bytes32 commitment, uint64 nproofs, uint64 timestamp)
    {
        Attestation memory a = attestations[settlementId][verifier];
        return (a.commitment, a.nproofs, a.timestamp);
    }
}
