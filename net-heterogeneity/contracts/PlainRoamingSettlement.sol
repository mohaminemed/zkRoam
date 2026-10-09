// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/*
    PlainRoamingSettlement
    -------------------------------------------------------------------
    BASELINE for the zkRoam benchmark: verify a roaming session
    "plainly", i.e. with NO zero-knowledge proof and NO aggregation.

    The full Call Detail Record (CDR) goes on-chain in clear as calldata
    and the contract re-executes the billing logic itself:

        charge == (endTime - startTime) * ratePerSecond
                  + volumeKB * ratePerKB

    using the tariff agreed on-chain for the (vmno, hmno) pair. This is
    what Groth16Verifier.verifyProof() (individual leg) and
    SnarkPackAggregateAnchor (aggregate leg) are compared against.

    ASSUMPTION - READ THIS: the real CDR circuit's constraints are not
    in the uploaded files (CDRVerifier.sol is snarkjs output with 5
    opaque public signals). The CDR layout and the billing rule below
    are a stand-in. Replace `_validate` with the exact logic your
    circuit enforces before drawing conclusions from the gas numbers;
    the benchmark harness does not need to change.

    Three entry points, matching the three ways the harness calls it:

      verifyPlain(cdr)   view, never reverts, returns bool.
                         Direct counterpart of verifyProof() (also a
                         view fn): validation cost only, no state.
      settlePlain(cdr)   validate + replay guard + record + accumulate
                         what the visited operator is owed.
      settleBatch(cdrs)  same as settlePlain for many CDRs in ONE tx
                         (the plain alternative to aggregation: one tx
                         per VMNO, but calldata grows linearly).

    Like SnarkPackAggregateAnchor.relayAggregateProof, settle* has no
    msg.sender restriction, so submission load can be spread over any
    pool of accounts. Operator authentication (e.g. a signature by the
    VMNO over the CDR) is deliberately NOT modeled; it would add a
    constant ecrecover cost per CDR.
    -------------------------------------------------------------------
*/

contract PlainRoamingSettlement {

    struct CDR {
        bytes32 sessionId;   // unique roaming-session id (replay guard key)
        uint32  vmno;        // visited operator (the one billing)
        uint32  hmno;        // home operator (the one being billed)
        uint64  startTime;   // unix seconds
        uint64  endTime;     // unix seconds
        uint64  volumeKB;    // data volume consumed in the session
        uint128 charge;      // claimed charge, in tariff units
    }

    struct Tariff {
        uint64 ratePerSecond;
        uint64 ratePerKB;
        bool   active;
    }

    // Reason codes returned by _validate (0 = valid).
    uint8 internal constant OK                = 0;
    uint8 internal constant ERR_SAME_OPERATOR = 1;
    uint8 internal constant ERR_NO_TARIFF     = 2;
    uint8 internal constant ERR_BAD_TIME      = 3;
    uint8 internal constant ERR_TOO_LONG      = 4;
    uint8 internal constant ERR_BAD_CHARGE    = 5;

    uint64  public constant MAX_SESSION_SECONDS = 1 days;
    uint256 public constant MAX_BATCH_SIZE      = 200;

    address public owner;

    // key(vmno, hmno) => agreed tariff
    mapping(uint64 => Tariff) public tariffs;
    // sessionId => keccak256(abi.encode(cdr)); non-zero means settled
    mapping(bytes32 => bytes32) public settledHash;
    // key(vmno, hmno) => total charge the hmno owes the vmno
    mapping(uint64 => uint256) public owed;

    event TariffSet(uint32 indexed vmno, uint32 indexed hmno, uint64 ratePerSecond, uint64 ratePerKB);
    event SessionSettled(
        bytes32 indexed sessionId,
        uint32 indexed vmno,
        uint32 indexed hmno,
        uint128 charge
    );

    error InvalidCDR(uint8 reason, uint256 index);
    error AlreadySettled(bytes32 sessionId);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    // ------------------------------------------------------------------
    // Admin
    // ------------------------------------------------------------------

    function setTariff(
        uint32 vmno,
        uint32 hmno,
        uint64 ratePerSecond,
        uint64 ratePerKB
    ) external onlyOwner {
        require(vmno != hmno, "same operator");
        tariffs[_key(vmno, hmno)] = Tariff({
            ratePerSecond: ratePerSecond,
            ratePerKB: ratePerKB,
            active: true
        });
        emit TariffSet(vmno, hmno, ratePerSecond, ratePerKB);
    }

    // ------------------------------------------------------------------
    // Verification / settlement
    // ------------------------------------------------------------------

    /// @notice Stateless check, direct counterpart of Groth16Verifier.verifyProof.
    /// Returns false instead of reverting, so a bad CDR still yields a
    /// successful transaction (same behaviour as verifyProof on a bad proof).
    function verifyPlain(CDR calldata c) external view returns (bool) {
        return _validate(c) == OK;
    }

    /// @notice Validate one CDR and record the settlement.
    function settlePlain(CDR calldata c) external {
        _settle(c, 0);
    }

    /// @notice Validate and record many CDRs in a single transaction.
    /// Reverts as a whole if any CDR is invalid or already settled.
    function settleBatch(CDR[] calldata cdrs) external {
        uint256 n = cdrs.length;
        require(n > 0, "empty batch");
        require(n <= MAX_BATCH_SIZE, "batch too large");
        for (uint256 i = 0; i < n; ++i) {
            _settle(cdrs[i], i);
        }
    }

    function isSettled(bytes32 sessionId) external view returns (bool) {
        return settledHash[sessionId] != bytes32(0);
    }

    /// @notice Charge the contract expects for a CDR's usage, or 0 if no tariff.
    function expectedCharge(uint32 vmno, uint32 hmno, uint64 startTime, uint64 endTime, uint64 volumeKB)
        external
        view
        returns (uint256)
    {
        Tariff memory t = tariffs[_key(vmno, hmno)];
        if (!t.active || endTime <= startTime) return 0;
        return _charge(t, startTime, endTime, volumeKB);
    }

    // ------------------------------------------------------------------
    // Internals
    // ------------------------------------------------------------------

    function _settle(CDR calldata c, uint256 index) internal {
        uint8 code = _validate(c);
        if (code != OK) revert InvalidCDR(code, index);

        if (settledHash[c.sessionId] != bytes32(0)) revert AlreadySettled(c.sessionId);

        settledHash[c.sessionId] = keccak256(abi.encode(c));
        owed[_key(c.vmno, c.hmno)] += c.charge;

        emit SessionSettled(c.sessionId, c.vmno, c.hmno, c.charge);
    }

    function _validate(CDR calldata c) internal view returns (uint8) {
        if (c.vmno == c.hmno) return ERR_SAME_OPERATOR;

        Tariff memory t = tariffs[_key(c.vmno, c.hmno)];
        if (!t.active) return ERR_NO_TARIFF;

        if (c.endTime <= c.startTime) return ERR_BAD_TIME;
        if (uint256(c.endTime) - uint256(c.startTime) > MAX_SESSION_SECONDS) return ERR_TOO_LONG;

        if (_charge(t, c.startTime, c.endTime, c.volumeKB) != uint256(c.charge)) {
            return ERR_BAD_CHARGE;
        }
        return OK;
    }

    function _charge(Tariff memory t, uint64 startTime, uint64 endTime, uint64 volumeKB)
        internal
        pure
        returns (uint256)
    {
        uint256 duration = uint256(endTime) - uint256(startTime);
        return duration * uint256(t.ratePerSecond) + uint256(volumeKB) * uint256(t.ratePerKB);
    }

    function _key(uint32 vmno, uint32 hmno) internal pure returns (uint64) {
        return (uint64(vmno) << 32) | uint64(hmno);
    }
}
