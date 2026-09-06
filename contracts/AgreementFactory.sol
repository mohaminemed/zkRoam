// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Agreement} from "./Agreement.sol";

contract AgreementFactory {
    using ECDSA for bytes32;
    
    struct AgreementRecord {
        address contractAddress;
        uint256 expiry;
    }


    // Mapping key: keccak256(abi.encode(sortedMno1, sortedMno2))
    mapping(bytes32 => AgreementRecord) public agreements;
    mapping (bytes32 => uint) public numberOfAgreements;
    address public immutable usdc;
    address public groth16Verifier;
    
    event AgreementCreated(
        address indexed mno1,
        address indexed mno2,
        address agreementAddress,
        uint256 expiry
    );

    constructor(address _usdc, address _groth16Verifier) {
        usdc = _usdc;
        groth16Verifier = _groth16Verifier;
    }

    function createAgreement(
        address[2] calldata mnos,
        uint256[3] calldata rates,
        uint256 expiry,
        bytes[2] calldata signatures
    ) external returns(address){
        (address sortedMno1, address sortedMno2) = _sortAddresses(mnos[0], mnos[1]);
        bytes32 agreementKey = keccak256(abi.encode(sortedMno1, sortedMno2));
        
        _validateCreationParams(sortedMno1, sortedMno2, rates, expiry, agreementKey);
        _validateSignatures(sortedMno1, sortedMno2, rates, expiry, signatures, agreementKey);
        
        Agreement newAgreement = _deployAgreement(sortedMno1, sortedMno2, rates, expiry);
        _storeAgreement(agreementKey, address(newAgreement), expiry);

        return address(newAgreement);
    }

    // TODO: Implement delete an agreement (we need to check that there is no active roaming session)

    function _validateCreationParams(
        address mno1,
        address mno2,
        uint256[3] calldata rates,
        uint256 expiry,
        bytes32 agreementKey
    ) private view {
        require(mno1 != address(0) && mno2 != address(0), "Zero address");
        require (mno1 != mno2, "Same MNO address");
        require(expiry > block.timestamp, "Expiry in past");
        
        AgreementRecord memory existing = agreements[agreementKey];
        require(existing.expiry == 0 || existing.expiry < block.timestamp, "Active agreement exists");
    }

    function _validateSignatures(
        address mno1,
        address mno2,
        uint256[3] calldata rates,
        uint256 expiry,
        bytes[2] calldata signatures,
        bytes32 agreementKey
    ) private {
        uint agreementNum = ++numberOfAgreements[agreementKey];
        bytes32 messageHash = keccak256(
            abi.encode(
                address(this),  
                mno1,
                mno2,
                rates,
                expiry,
                agreementNum
            )
        );

        address[2] memory signers = [
            messageHash.recover(signatures[0]),
            messageHash.recover(signatures[1])
        ];

        (address signerA, address signerB) = _sortAddresses(signers[0], signers[1]);

        require(signerA == mno1 && signerB == mno2, "Invalid signatures");
    }

    function _deployAgreement(
        address mno1,
        address mno2,
        uint256[3] calldata rates,
        uint256 expiry
    ) private returns (Agreement) {
        Agreement.Rates memory agreementRates = Agreement.Rates({
            sms: rates[0],
            voice: rates[1],
            data: rates[2]
        });
        
        return new Agreement(mno1, mno2, agreementRates, expiry, usdc, groth16Verifier);
    }

    function _storeAgreement(
        bytes32 key,
        address agreement,
        uint256 expiry
    ) private {
        agreements[key] = AgreementRecord({
            contractAddress: agreement,
            expiry: expiry
        });

        
        emit AgreementCreated(
            agreements[key].contractAddress != address(0) ? 
                Agreement(agreement).mno1() : address(0),
            agreements[key].contractAddress != address(0) ? 
                Agreement(agreement).mno2() : address(0),
            agreement,
            expiry
        );
    }

    function _sortAddresses(address a, address b) 
        private pure returns (address, address) 
    {
        return a < b ? (a, b) : (b, a);
    }

    // ==================== Utility Functions ====================
    function getActiveAgreement(address mno1, address mno2) 
        external view returns (address) 
    {
        (address a, address b) = _sortAddresses(mno1, mno2);
        AgreementRecord memory record = agreements[keccak256(abi.encode(a, b))];
        return record.expiry > block.timestamp ? record.contractAddress : address(0);
    }
}