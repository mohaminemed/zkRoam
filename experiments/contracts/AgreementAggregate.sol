// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./AgreementBase.sol";

interface ISnarkPackAggregateAnchor {
    function isFinalized(bytes32 settlementId) external view returns (bool);
    function finalizedCommitment(bytes32 settlementId) external view returns (bytes32);
}

/// @notice Aggregate path: N sessions settled in one tx against a SnarkPack
/// aggregate that threshold-many authorised verifiers attested off-chain via
/// SnarkPackAggregateAnchor. There is NO per-session commit (submitCDR).
///
/// What the attesters sign (the anchor's `commitment`):
///
///   keccak256(abi.encode(
///       BATCH_DOMAIN, address(this), chainid, batchId, n, aggProofHash,
///       rates.sms, rates.data, rates.voice,
///       keccak256( for i in 0..n: sessionId_i || cost_i || cdrHash_i )))
///
/// The contract RECOMPUTES that value from calldata + its own state, so:
///  * cost_i is the exact value inside the proved public inputs, per session;
///  * the amount paid out is derived from those same cost_i, never passed
///    separately, so it cannot diverge from the sum of the proved usages;
///  * the batch is bound to this agreement, this chain, and this batchId;
///  * the rates the proof was made against are the contract's current rates.
contract AgreementAggregate is AgreementBase {
    using SafeERC20 for IERC20;

    ISnarkPackAggregateAnchor public immutable anchor;

    bytes32 public constant BATCH_DOMAIN = keccak256("zkRoam.AgreementAggregate.batch.v1");

    event BatchSettled(
        bytes32 indexed batchId,
        uint256 nSessions,
        uint256 totalActual,
        uint256 totalRefunded
    );

    error BadBatch();
    error BatchNotAnchored();

    constructor(
        address _mno1,
        address _mno2,
        Rates memory _rates,
        uint256 _expiry,
        address _paymentToken,
        address _anchor
    ) AgreementBase(_mno1, _mno2, _rates, _expiry, _paymentToken) {
        require(_anchor != address(0), "Invalid anchor");
        anchor = ISnarkPackAggregateAnchor(_anchor);
    }

    /// @notice settlementId the verifiers must attest under on the anchor.
    /// Scoped to this agreement so ids from other agreements cannot collide.
    function settlementIdOf(bytes32 batchId) public view returns (bytes32) {
        return keccak256(abi.encode(address(this), batchId));
    }

    /// @notice Commitment verifiers must attest to, for off-chain use.
    function batchCommitment(
        bytes32 batchId,
        bytes32 aggProofHash,
        uint256[] calldata sessionIds,
        uint256[] calldata costs,
        bytes32[] calldata cdrHashes
    ) external view returns (bytes32) {
        return _commitment(batchId, aggProofHash, sessionIds.length, _digest(sessionIds, costs, cdrHashes));
    }

    /// @param batchId      caller-chosen unique id; attested as settlementIdOf(batchId)
    /// @param aggProofHash keccak256 of the aggregate proof bytes verified off-chain
    /// @param sessionIds   sessions in the batch, in the order proved
    /// @param costs        proved actual cost of each session (public input 0 of each proof)
    /// @param cdrHashes    CDR hash of each session (public input 1 of each proof)
    /// @param ueSigs       EIP-2098 signatures for each session
    function settleBatch(
        bytes calldata ueSigs,
        bytes32 batchId,
        bytes32 aggProofHash,
        uint256[] calldata sessionIds,
        uint256[] calldata costs,
        bytes32[] calldata cdrHashes
    ) external {
        uint256 n = sessionIds.length;
        if (n == 0 || costs.length != n || cdrHashes.length != n) revert BadBatch();

        _checkUESignatures(ueSigs, sessionIds, cdrHashes);

        // 1. Per-session eligibility + effects. Duplicates revert: the first
        //    occurrence flips the session to COMPLETED.
        uint256 due1;   // owed to mno1 (as VMNO: cost; as HMNO: refund)
        uint256 due2;   // owed to mno2
        uint256 totalActual;
        {
            address m1 = mno1;
            for (uint256 i = 0; i < n; ++i) {
                (address hmno, uint256 est) = _settleSession(sessionIds[i], costs[i]);
                uint256 cost = costs[i];
                totalActual += cost;
                if (hmno == m1) { due2 += cost; due1 += est - cost; }
                else            { due1 += cost; due2 += est - cost; }
            }
        }

        // 2. Batch must be exactly what the verifiers attested.
        bytes32 sid = settlementIdOf(batchId);
        if (!anchor.isFinalized(sid)) revert BatchNotAnchored();
        if (anchor.finalizedCommitment(sid) != _commitment(batchId, aggProofHash, n, _digest(sessionIds, costs, cdrHashes))) {
            revert BatchNotAnchored();
        }

        // 3. Interactions last. At most two transfers for the whole batch.
        _payout(due1, due2);

        emit BatchSettled(batchId, n, totalActual, due1 + due2 - totalActual);
    }

    // ------------------------------------------------------------------

    function _digest(
        uint256[] calldata sessionIds,
        uint256[] calldata costs,
        bytes32[] calldata cdrHashes
    ) internal pure returns (bytes32 d) {
        uint256 n = sessionIds.length;
        bytes memory buf = new bytes(n * 96);
        for (uint256 i = 0; i < n; ++i) {
            uint256 id = sessionIds[i];
            uint256 cost = costs[i];
            bytes32 cdr = cdrHashes[i];
            assembly {
                let p := add(add(buf, 0x20), mul(i, 0x60))
                mstore(p, id)
                mstore(add(p, 0x20), cost)
                mstore(add(p, 0x40), cdr)
            }
        }
        d = keccak256(buf);
    }

    function _commitment(bytes32 batchId, bytes32 aggProofHash, uint256 n, bytes32 sessionsDigest)
        internal
        view
        returns (bytes32)
    {
        Rates memory r = rates;
        return keccak256(abi.encode(
            BATCH_DOMAIN, address(this), block.chainid, batchId, n, aggProofHash,
            r.sms, r.data, r.voice, sessionsDigest
        ));
    }

bytes32 public constant UE_CDR_DOMAIN = keccak256("zkRoam.AgreementAggregate.ueCDR.v1");

error BadUESignature(uint256 sessionId);

/// @notice Digest the UE signs for each session (the aggregate-path replacement of submitCDR).
function ueCDRDigest(uint256 sessionId, bytes32 cdrHash) public view returns (bytes32) {
    return keccak256(abi.encode(UE_CDR_DOMAIN, address(this), block.chainid, sessionId, cdrHash));
}

/// @param ueSigs 64 bytes per session (r || vs, EIP-2098), same order as sessionIds.
/// NOTE: ueSigs is deliberately the FIRST parameter: it is only used at the start, and
/// keeping it deepest on the stack leaves the depth of every other variable unchanged
/// (the function was already close to "stack too deep").


function _checkUESignatures(
    bytes calldata ueSigs,
    uint256[] calldata sessionIds,
    bytes32[] calldata cdrHashes
) internal view {
    uint256 n = sessionIds.length;
    if (ueSigs.length != n * 64) revert BadBatch();
    for (uint256 i = 0; i < n; ++i) {
        uint256 id = sessionIds[i];
        bytes32 r  = bytes32(ueSigs[i * 64 : i * 64 + 32]);
        bytes32 vs = bytes32(ueSigs[i * 64 + 32 : i * 64 + 64]);
        if (ECDSA.recover(ueCDRDigest(id, cdrHashes[i]), r, vs) != sessions[id].ueAddress) {
            revert BadUESignature(id);
        }
    }
}

}

