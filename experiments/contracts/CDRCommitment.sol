// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title CDRCommitment
/// @notice Per-session commitment of a CDR hash: commit(sid, h_cdr).
///         One storage slot per session (sid -> h_cdr), write-once.
///         The ZK settlement (verifyProof) can later be
///         bound to the committed h_cdr by reading commitments[sid].
contract CDRCommitment {
    mapping(bytes32 => bytes32) public commitments;

    event CDRCommitted(bytes32 indexed sid, bytes32 hCdr);

    error AlreadyCommitted(bytes32 sid);
    error ZeroHash();

    function commit(bytes32 sid, bytes32 hCdr) external {
        if (hCdr == bytes32(0)) revert ZeroHash();
        if (commitments[sid] != bytes32(0)) revert AlreadyCommitted(sid);
        commitments[sid] = hCdr; // SSTORE 0 -> nonzero (20,000 + 2,100 cold)
        emit CDRCommitted(sid, hCdr); // LOG2: 375 + 2*375 + 8*32 = 1,381
    }
}
