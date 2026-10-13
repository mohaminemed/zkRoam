// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./AgreementBase.sol";

/// @notice Plain path: identical escrow, session state, per-session cost bound,
/// events and payouts to the ZK paths. The ONLY difference is how the cost is
/// validated: the full CDR goes on-chain in clear and the contract
///   1. checks keccak256(abi.encode(cdr)) == the hash the UE committed, and
///   2. recomputes cost = sms*r.sms + voice*r.voice + data*r.data itself.
/// No proof, no aggregation.
///
/// ASSUMPTION (not in the uploaded files): the CDR fields and billing formula
/// below are a stand-in for what the real circuit proves. Replace `CDR` and
/// `_cost` with the circuit's exact rule before drawing conclusions from the
/// gas numbers. The UE commits keccak256(abi.encode(cdr)); if the circuit's
/// cdrHash is Poseidon the plain path cannot reuse it on the EVM.
contract AgreementPlain is AgreementBase {
    using SafeERC20 for IERC20;

    struct CDR {
        uint64 sms;           // number of SMS
        uint64 voiceSeconds;  // voice duration
        uint64 dataKB;        // data volume
    }

    event CDRSubmitted(uint256 indexed sessionId, address indexed ue, bytes32 cdrHash);
    event BatchSettled(uint256 nSessions, uint256 totalActual, uint256 totalRefunded);

    error CDRNotCommitted(uint256 sessionId);
    error CDRMismatch(uint256 sessionId);
    error BadBatch();

    constructor(
        address _mno1,
        address _mno2,
        Rates memory _rates,
        uint256 _expiry,
        address _paymentToken
    ) AgreementBase(_mno1, _mno2, _rates, _expiry, _paymentToken) {}

    /// @dev identical to AgreementIndividual.submitCDR
    function submitCDR(uint256 sessionId, bytes32 cdrHash) external {
        Session storage s = sessions[sessionId];
        require(msg.sender == s.ueAddress, "Only UE can supply CDR hash of his roaming session");
        require(s.status == SessionStatus.PENDING, "Session not pending");
        require(cdrHash != bytes32(0), "Empty CDR hash");
        require(s.ueCDRHash == bytes32(0), "CDR hash for this session is already set");
        s.ueCDRHash = cdrHash;
        emit CDRSubmitted(sessionId, msg.sender, cdrHash);
    }

    function terminateRoamingSession(uint256 sessionId, CDR calldata cdr) external {
        Session storage s = sessions[sessionId];
        if (s.status != SessionStatus.PENDING) revert InvalidSessionState(sessionId);

        bytes32 committed = s.ueCDRHash;
        if (committed == bytes32(0)) revert CDRNotCommitted(sessionId);
        if (keccak256(abi.encode(cdr)) != committed) revert CDRMismatch(sessionId);

        uint256 estimated = s.estimatedCost;
        uint256 cost = _cost(cdr, rates);
        if (cost > estimated) revert CostExceedsEstimate(sessionId);

        address hmno = s.hmno;
        address vmno = _counterparty(hmno);
        _finalize(s, cost);

        uint256 refund = estimated - cost;
        paymentToken.safeTransfer(vmno, cost);
        if (refund != 0) paymentToken.safeTransfer(hmno, refund);

        emit SessionTerminated(sessionId, vmno, cost, refund);
    }

    /// @notice Plain alternative to aggregation: many sessions, one tx, CDRs
    /// in clear. Same netting and at most two transfers as the aggregate path.
    function settleBatch(uint256[] calldata sessionIds, CDR[] calldata cdrs) external {
        uint256 n = sessionIds.length;
        if (n == 0 || cdrs.length != n) revert BadBatch();

        Rates memory r = rates;
        address m1 = mno1;
        uint256 due1;
        uint256 due2;
        uint256 totalActual;
        for (uint256 i = 0; i < n; ++i) {
            uint256 id = sessionIds[i];
            bytes32 committed = sessions[id].ueCDRHash;
            if (committed == bytes32(0)) revert CDRNotCommitted(id);
            if (keccak256(abi.encode(cdrs[i])) != committed) revert CDRMismatch(id);

            uint256 cost = _cost(cdrs[i], r);
            (address hmno, uint256 est) = _settleSession(id, cost);
            totalActual += cost;
            if (hmno == m1) { due2 += cost; due1 += est - cost; }
            else            { due1 += cost; due2 += est - cost; }
        }
        _payout(due1, due2);
        emit BatchSettled(n, totalActual, due1 + due2 - totalActual);
    }

    function hashCDR(CDR calldata cdr) external pure returns (bytes32) {
        return keccak256(abi.encode(cdr));
    }

    function _cost(CDR calldata cdr, Rates memory r) internal pure returns (uint256) {
        return uint256(cdr.sms) * r.sms + uint256(cdr.voiceSeconds) * r.voice + uint256(cdr.dataKB) * r.data;
    }
}
