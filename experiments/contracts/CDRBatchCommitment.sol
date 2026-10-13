// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title CDRBatchCommitment
/// @notice Amortized variant of CDRCommitment for the AGGREGATE path.
///         One tx per settlement batch (one VMNO, `count` sessions) commits the
///         Merkle root of its (sid, h_cdr) pairs: one SSTORE + one LOG for the
///         whole batch instead of one per session.
///
///         Tree convention (must match the off-chain builder):
///           leaf   = keccak256(keccak256(abi.encode(sid, hCdr)))   // double hash
///           parent = keccak256(min(a, b) || max(a, b))             // sorted pair
///           odd node on a level is promoted unchanged to the next level
///         Sorted-pair hashing means an inclusion proof is just the sibling list
///         (no index), of length ceil(log2(count)).
///
/// @dev Differences vs. CDRCommitment (to state in the paper):
///      - sid uniqueness is NOT enforced across batches (only batchId is
///        write-once); a duplicate sid inside a batch is not detected on-chain.
///      - `count` is only emitted in the event, not stored (one slot per batch).
///      - batchId is caller-chosen and unauthenticated (same trust model as
///        CDRCommitment.sid); bind it to the committer off-chain, e.g.
///        keccak256(committer, epoch, vmnoId), if front-running matters.
///      - the commitment is made BEFORE settlement, so the timestamp guarantee
///        of the per-session commit is kept, but only at batch granularity.
contract CDRBatchCommitment {
    mapping(bytes32 => bytes32) public batchRoots;

    event BatchCommitted(bytes32 indexed batchId, bytes32 root, uint32 count);

    error AlreadyCommitted(bytes32 batchId);
    error ZeroRoot();
    error ZeroCount();

    function commitBatch(bytes32 batchId, bytes32 root, uint32 count) external {
        if (root == bytes32(0)) revert ZeroRoot();
        if (count == 0) revert ZeroCount();
        if (batchRoots[batchId] != bytes32(0)) revert AlreadyCommitted(batchId);
        batchRoots[batchId] = root;          // SSTORE 0 -> nonzero (20,000 + 2,100 cold)
        emit BatchCommitted(batchId, root, count); // LOG2: 375 + 2*375 + 8*64 = 1,637
    }

    function leafOf(bytes32 sid, bytes32 hCdr) public pure returns (bytes32) {
        return keccak256(bytes.concat(keccak256(abi.encode(sid, hCdr))));
    }

    /// @notice True iff (sid, hCdr) is a leaf of the batch committed under batchId.
    ///         A settlement contract can call this to bind a session to its commitment.
    function verifyInclusion(
        bytes32 batchId,
        bytes32 sid,
        bytes32 hCdr,
        bytes32[] calldata proof
    ) external view returns (bool) {
        bytes32 root = batchRoots[batchId];
        if (root == bytes32(0)) return false;
        bytes32 node = leafOf(sid, hCdr);
        for (uint256 i = 0; i < proof.length; ++i) {
            bytes32 sibling = proof[i];
            node = node < sibling ? _hashPair(node, sibling) : _hashPair(sibling, node);
        }
        return node == root;
    }

    function _hashPair(bytes32 a, bytes32 b) private pure returns (bytes32 h) {
        assembly {
            mstore(0x00, a)
            mstore(0x20, b)
            h := keccak256(0x00, 0x40)
        }
    }
}
