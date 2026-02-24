// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import "./Groth16Verifier.sol";

contract Agreement {
    using ECDSA for bytes32;
    using SafeERC20 for IERC20;
    Groth16Verifier public groth16Verifier;
    
    enum SessionStatus { UNKOWN, PENDING, COMPLETED }

    struct Session {
        address ueAddress;
        address hmno;
        uint256 estimatedCost;
        uint256 actualCost;
        uint256 startTime;
        uint256 endTime;
        bytes32 ueCDRHash;
        SessionStatus status;
    }

    struct Rates {
        uint256 sms;
        uint256 voice;  
        uint256 data; 
    }

    // Immutable agreement parameters
    address public immutable mno1;
    address public immutable mno2;
    Rates public rates;
    IERC20 public paymentToken;
    uint256 public immutable expiry;

    // Session management
    uint256 public sessionCounter;
    mapping(uint256 => Session) public sessions;
    mapping(address => uint256) public nonces;
    mapping(address => uint[]) public ueRoamingSessions;

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

    event CDRSubmitted(
        address indexed ue,
        bytes32 cdrHash
    );

    event PaymentTokenChanged(address indexed oldPaymentToken, address indexed newPaymentToken);

    error InvalidProof();
    error CostMismatch();
    error InvalidSessionState();

    constructor(
        address _mno1,
        address _mno2,
        Rates memory _rates,
        uint256 _expiry,
        address _paymentToken,
        address _groth16Verifier
    ) {
        require(_mno1 != _mno2, "Identical MNOs");
        require(_expiry > block.timestamp, "Expired agreement");
        require(_paymentToken != address(0), "Invalid payment token");

        mno1 = _mno1;
        mno2 = _mno2;
        paymentToken = IERC20(_paymentToken);
        rates = _rates;
        expiry = _expiry;
        groth16Verifier = Groth16Verifier(_groth16Verifier);

        emit PaymentTokenChanged(address(0), _paymentToken);
    }

    function startRoamingSession(
        address ueAddress,
        uint256 estimatedCost,
        uint256 nonce,
        bytes calldata signature
    ) external returns(uint){
        require(block.timestamp < expiry, "Agreement expired");
        require(ueAddress != address(0), "Invalid UE address");
        require(estimatedCost > 0, "Invalid estimated cost");
        uint lastRoamingSession = getLatestRoamingSession(ueAddress);
        if (lastRoamingSession != 0) {
            SessionStatus lastRoamingSessionStatus = sessions[lastRoamingSession - 1].status;
            require(lastRoamingSessionStatus == SessionStatus.UNKOWN || lastRoamingSessionStatus == SessionStatus.COMPLETED, "Ongoing active session for UE");
        }
        
        bytes32 messageHash = keccak256(
            abi.encode(
                address(this),
                ueAddress,
                estimatedCost,
                nonce
            )
        );
        // Determine HMNO address from signature    
        address hmno = _recoverSigner(messageHash, signature);
        
        // Validate HMNO is part of this agreement
        require(hmno == mno1 || hmno == mno2, "Unauthorized HMNO");
        require(hmno != address(0), "Invalid HMNO address");

        address vmno = hmno == mno1 ? mno2 : mno1;

        // Check and update nonce
        require(nonces[hmno] == nonce, "Invalid nonce");
        nonces[hmno]++;

        // Transfer estimated funds
        _transferFunds(hmno, estimatedCost);

        // Create session record
        sessionCounter++;
        ueRoamingSessions[ueAddress].push(sessionCounter);
        sessions[sessionCounter] = Session({
            ueAddress: ueAddress,
            hmno: hmno,
            estimatedCost: estimatedCost,
            actualCost: 0,
            startTime: block.timestamp,
            endTime: 0,
            ueCDRHash: bytes32(0),
            status: SessionStatus.PENDING
        });

        emit SessionStarted(sessionCounter, hmno, vmno, ueAddress, estimatedCost);

        return sessionCounter;
    }

    function submitCDR(uint sessionID, bytes32 cdrHash) external {
        Session storage session = sessions[sessionID];
        require(msg.sender == session.ueAddress, "Only UE can supply CDR hash of his roaming session");
        require(session.ueCDRHash == bytes32(0), "CDR hash for this session is already set");
        session.ueCDRHash = cdrHash;

        emit CDRSubmitted(msg.sender, cdrHash);
    }


    function terminateRoamingSession(
        uint256 sessionId,
        uint256[2] memory pointA_,
        uint256[2][2] memory pointB_,
        uint256[2] memory pointC_,
        uint256 totalCost
    ) external {
        Session storage session = sessions[sessionId];
        
        // Validate session state
        if(session.status != SessionStatus.PENDING) {
            revert InvalidSessionState();
        }
        if(session.ueCDRHash == bytes32(0)) {
            revert("CDR hash not commited by the UE");
        }

        // No need to verify that the caller is the VMNO since the proof can be generated only by the VMNO and it is okay for anyone to call the function
        // as long as the proof is valid.

        // Prepare public inputs for proof verification
        uint256[5] memory publicInputs;
        publicInputs[0] = totalCost;
        publicInputs[1] = uint256(session.ueCDRHash);
        publicInputs[2] = rates.sms;
        publicInputs[3] = rates.data;
        publicInputs[4] = rates.voice;

        // Verify ZK proof
        if(!groth16Verifier.verifyProof(pointA_, pointB_, pointC_, publicInputs)) {
            revert InvalidProof();
        }

        // Validate total cost consistency
        if(totalCost > session.estimatedCost) {
            revert CostMismatch();
        }

        // Calculate final settlement
        
        address vmno = session.hmno == mno1 ? mno2: mno1;
        // Calculate final settlement
        uint256 refundedAmount = _settlePayment(session.hmno, vmno, session.estimatedCost, totalCost);

        // Update session state
        session.actualCost = totalCost;
        session.endTime = block.timestamp;
        session.status = SessionStatus.COMPLETED;

        emit SessionTerminated(sessionId, vmno, totalCost, refundedAmount);
    }

    function _settlePayment(
        address hmno,
        address vmno,
        uint256 estimated,
        uint256 actual
    ) internal returns (uint256) {
        uint256 balance = paymentToken.balanceOf(address(this));
        
        paymentToken.safeTransfer(vmno, actual);
        if(actual < estimated) {
            // Release actual amount, refund difference
            paymentToken.safeTransfer(hmno, estimated - actual);
            return actual;
        }

        return 0;
    }

    function getLatestRoamingSession(address ue) public view returns (uint) {
        uint length = ueRoamingSessions[ue].length;
        return length > 0 ? ueRoamingSessions[ue][length - 1] : 0;
    }
    
    // TODO: implement changePaymentToken

    function getActiveSessions() external view returns (uint256[] memory) {
        uint256[] memory active = new uint256[](sessionCounter);
        uint256 count;
        
        for(uint256 i = 1; i <= sessionCounter; i++) {
            if(sessions[i].status == SessionStatus.PENDING) {
                active[count] = i;
                count++;
            }
        }
        
        assembly { mstore(active, count) }
        return active;
    }

    function isValidHMNO(address account) public view returns (bool) {
        return account == mno1 || account == mno2;
    }

     function _recoverSigner(
        bytes32 messageHash,
        bytes calldata signature
    ) internal pure returns (address) {

        return messageHash.recover(signature);
    }

    function _transferFunds(address from, uint256 amount) internal {
        uint256 balanceBefore = paymentToken.balanceOf(address(this));
        paymentToken.safeTransferFrom(from, address(this), amount);
        uint256 balanceAfter = paymentToken.balanceOf(address(this));
        require(balanceAfter - balanceBefore == amount, "Transfer verification failed");
    }
}