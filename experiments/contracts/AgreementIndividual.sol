// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./AgreementBase.sol";
import "./CDRVerifier.sol";

/// @notice Individual path: UE commits h_cdr on-chain, then each session is
/// settled by its own Groth16 verifyProof call.
contract AgreementIndividual is AgreementBase {
    using SafeERC20 for IERC20;

    Groth16Verifier public immutable groth16Verifier;

    event CDRSubmitted(uint256 indexed sessionId, address indexed ue, bytes32 cdrHash);

    error InvalidProof();
    error CDRNotCommitted(uint256 sessionId);

    constructor(
        address _mno1,
        address _mno2,
        Rates memory _rates,
        uint256 _expiry,
        address _paymentToken,
        address _groth16Verifier
    ) AgreementBase(_mno1, _mno2, _rates, _expiry, _paymentToken) {
        require(_groth16Verifier != address(0), "Invalid verifier");
        groth16Verifier = Groth16Verifier(_groth16Verifier);
    }

    function submitCDR(uint256 sessionId, bytes32 cdrHash) external {
        Session storage s = sessions[sessionId];
        require(msg.sender == s.ueAddress, "Only UE can supply CDR hash of his roaming session");
        require(s.status == SessionStatus.PENDING, "Session not pending");
        require(cdrHash != bytes32(0), "Empty CDR hash");
        require(s.ueCDRHash == bytes32(0), "CDR hash for this session is already set");
        s.ueCDRHash = cdrHash;
        emit CDRSubmitted(sessionId, msg.sender, cdrHash);
    }

    function terminateRoamingSession(
        uint256 sessionId,
        uint256[2] calldata pointA_,
        uint256[2][2] calldata pointB_,
        uint256[2] calldata pointC_,
        uint256 totalCost
    ) external {
        Session storage s = sessions[sessionId];
        if (s.status != SessionStatus.PENDING) revert InvalidSessionState(sessionId);

        bytes32 cdrHash = s.ueCDRHash;
        if (cdrHash == bytes32(0)) revert CDRNotCommitted(sessionId);

        uint256 estimated = s.estimatedCost;
        if (totalCost > estimated) revert CostExceedsEstimate(sessionId);

        // Anyone may call: only the VMNO can produce a valid proof, and funds
        // only ever go to the two agreement parties.
        // Order MUST match the circuit's public signals (snarkjs public.json):
        // [r_sms, r_data, r_voice, cost, cdrHash]
        uint256[5] memory publicInputs = [
            rates.sms,
            rates.data,
            rates.voice,
            totalCost,
            uint256(cdrHash)
        ];
        if (!groth16Verifier.verifyProof(pointA_, pointB_, pointC_, publicInputs)) revert InvalidProof();

        // Effects before interactions.
        address hmno = s.hmno;
        address vmno = _counterparty(hmno);
        _finalize(s, totalCost);

        uint256 refund = estimated - totalCost;
        paymentToken.safeTransfer(vmno, totalCost);
        if (refund != 0) paymentToken.safeTransfer(hmno, refund);

        emit SessionTerminated(sessionId, vmno, totalCost, refund);
    }
}
