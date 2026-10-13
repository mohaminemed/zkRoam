// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

/// @notice Everything the individual and aggregate settlement paths share:
/// agreement parameters, session start + escrow, and session bookkeeping.
/// Both paths inherit this unchanged, so the only gas difference between
/// them is the settlement logic itself.
abstract contract AgreementBase {
    using ECDSA for bytes32;
    using SafeERC20 for IERC20;

    enum SessionStatus { UNKNOWN, PENDING, COMPLETED }

    struct Session {
        address ueAddress;
        address hmno;
        uint256 estimatedCost;
        uint256 actualCost;
        uint256 startTime;
        uint256 endTime;
        bytes32 ueCDRHash;      // only written by the individual path
        SessionStatus status;
    }

    struct Rates {
        uint256 sms;
        uint256 voice;
        uint256 data;
    }

    address public immutable mno1;
    address public immutable mno2;
    uint256 public immutable expiry;
    Rates public rates;
    IERC20 public paymentToken;

    uint256 public sessionCounter;
    mapping(uint256 => Session) public sessions;
    mapping(address => uint256) public nonces;
    mapping(address => uint256[]) public ueRoamingSessions;

    event SessionStarted(
        uint256 indexed sessionId,
        address indexed hmno,
        address indexed vmno,
        address ueAddress,
        uint256 estimatedCost
    );
    event SessionTerminated(
        uint256 indexed sessionId,
        address indexed vmno,
        uint256 actualCost,
        uint256 refundedAmount
    );
    event PaymentTokenChanged(address indexed oldPaymentToken, address indexed newPaymentToken);

    error InvalidSessionState(uint256 sessionId);
    error CostExceedsEstimate(uint256 sessionId);

    constructor(
        address _mno1,
        address _mno2,
        Rates memory _rates,
        uint256 _expiry,
        address _paymentToken
    ) {
        require(_mno1 != _mno2, "Identical MNOs");
        require(_mno1 != address(0) && _mno2 != address(0), "Zero MNO");
        require(_expiry > block.timestamp, "Expired agreement");
        require(_paymentToken != address(0), "Invalid payment token");

        mno1 = _mno1;
        mno2 = _mno2;
        paymentToken = IERC20(_paymentToken);
        rates = _rates;
        expiry = _expiry;

        emit PaymentTokenChanged(address(0), _paymentToken);
    }

    // ------------------------------------------------------------------
    // Session start (identical for both settlement paths)
    // ------------------------------------------------------------------

    function startRoamingSession(
        address ueAddress,
        uint256 estimatedCost,
        uint256 nonce,
        bytes calldata signature
    ) external returns (uint256) {
        require(block.timestamp < expiry, "Agreement expired");
        require(ueAddress != address(0), "Invalid UE address");
        require(estimatedCost > 0, "Invalid estimated cost");

        // FIX: ids are 1-based and sessions[] is keyed by that same id. The
        // original read sessions[last - 1], i.e. a different session (possibly
        // another UE's), so the "one active session per UE" rule was unsound.
        uint256 last = getLatestRoamingSession(ueAddress);
        if (last != 0) {
            require(sessions[last].status == SessionStatus.COMPLETED, "Ongoing active session for UE");
        }

        bytes32 messageHash = keccak256(abi.encode(address(this), ueAddress, estimatedCost, nonce));
        address hmno = messageHash.recover(signature);
        require(hmno == mno1 || hmno == mno2, "Unauthorized HMNO");

        address vmno = hmno == mno1 ? mno2 : mno1;

        require(nonces[hmno] == nonce, "Invalid nonce");
        nonces[hmno]++;

        _transferFunds(hmno, estimatedCost);

        uint256 id = ++sessionCounter;
        ueRoamingSessions[ueAddress].push(id);
        sessions[id] = Session({
            ueAddress: ueAddress,
            hmno: hmno,
            estimatedCost: estimatedCost,
            actualCost: 0,
            startTime: block.timestamp,
            endTime: 0,
            ueCDRHash: bytes32(0),
            status: SessionStatus.PENDING
        });

        emit SessionStarted(id, hmno, vmno, ueAddress, estimatedCost);
        return id;
    }

    // ------------------------------------------------------------------
    // Shared helpers
    // ------------------------------------------------------------------

    /// @dev Terminal state write. PENDING -> COMPLETED is the ONLY transition
    /// out of PENDING and COMPLETED is never left, so a session can be settled
    /// at most once no matter which path or batch tries.
    function _finalize(Session storage s, uint256 cost) internal {
        s.actualCost = cost;
        s.endTime = block.timestamp;
        s.status = SessionStatus.COMPLETED;
    }


    /// @dev Shared by the batch paths (aggregate, plain-batch). Same guards as
    /// the individual path: session PENDING and cost <= that session's own
    /// escrow (a per-session bound, not just a batch-total bound).
    function _settleSession(uint256 id, uint256 cost) internal returns (address hmno, uint256 est) {
        Session storage s = sessions[id];
        if (s.status != SessionStatus.PENDING) revert InvalidSessionState(id);
        est = s.estimatedCost;
        if (cost > est) revert CostExceedsEstimate(id);
        hmno = s.hmno;
        _finalize(s, cost);
        emit SessionTerminated(id, _counterparty(hmno), cost, est - cost);
    }

    /// @dev Pay out a netted batch: at most two transfers for any batch size.
    function _payout(uint256 due1, uint256 due2) internal {
        if (due1 != 0) paymentToken.safeTransfer(mno1, due1);
        if (due2 != 0) paymentToken.safeTransfer(mno2, due2);
    }

    function _counterparty(address mno) internal view returns (address) {
        return mno == mno1 ? mno2 : mno1;
    }

    function _transferFunds(address from, uint256 amount) internal {
        uint256 balanceBefore = paymentToken.balanceOf(address(this));
        paymentToken.safeTransferFrom(from, address(this), amount);
        require(paymentToken.balanceOf(address(this)) - balanceBefore == amount, "Transfer verification failed");
    }

    function getLatestRoamingSession(address ue) public view returns (uint256) {
        uint256 length = ueRoamingSessions[ue].length;
        return length > 0 ? ueRoamingSessions[ue][length - 1] : 0;
    }

    function getActiveSessions() external view returns (uint256[] memory) {
        uint256[] memory active = new uint256[](sessionCounter);
        uint256 count;
        for (uint256 i = 1; i <= sessionCounter; i++) {
            if (sessions[i].status == SessionStatus.PENDING) {
                active[count++] = i;
            }
        }
        assembly { mstore(active, count) }
        return active;
    }

    function isValidHMNO(address account) public view returns (bool) {
        return account == mno1 || account == mno2;
    }
}
