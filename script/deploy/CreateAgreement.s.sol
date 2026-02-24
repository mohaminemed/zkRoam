// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Script.sol";
import "../../src/AgreementFactory.sol";
import {Vm} from "forge-std/Vm.sol";
import {console} from "forge-std/console.sol";

contract AgreementCreation is Script {
    function run() external {
        AgreementFactory agreementFactory = AgreementFactory(0xF2F9cFd8f2Bf613D511606124fEb56E60077439E);

        uint256 mno1PrivateKey = 0x0b926be7e0e254c513b112e6a82d89cd53f109fa5ad0b8be3b60ba436afda483;
        address mno1Address = 0x46CC7efbC0fb7F80c037B33c7fe416692Ea1075B;
        uint256 mno2PrivateKey = 0x8b506681f432e9d3765c7452a37b063010939cc388784fee99a896e827338f32;
        address mno2Address = 0x58D85998a7c6ed077f9FB913700f5f5Da539a786;

        mno1Address = mno1Address < mno2Address ? mno1Address : mno2Address;
        mno2Address = mno1Address > mno2Address ? mno1Address : mno2Address;
        vm.startBroadcast(mno1PrivateKey);
        // create roaming agreement
        address[2] memory mnos;
        mnos[0] = mno1Address;
        mnos[1] = mno2Address;
        uint256[3] memory rates;
        rates[0] = 12000000;
        rates[1] = 12000000;
        rates[2] = 12000000;
        uint expiry = block.timestamp + 365 days;
        bytes32 messageHash = keccak256(abi.encode(address(agreementFactory), mno1Address, mno2Address, rates, expiry, 1));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(mno1PrivateKey, messageHash);
        bytes memory signatureMno1 = abi.encodePacked(r, s, v);  
        (v, r, s) = vm.sign(mno2PrivateKey, messageHash);
        bytes memory signatureMno2 = abi.encodePacked(r, s, v); 
        bytes[2] memory signatures;
        signatures[0] = signatureMno1;
        signatures[1] = signatureMno2;
        address agreement = agreementFactory.createAgreement(mnos, rates, expiry, signatures);
        console.log("Agreement address", agreement);
        vm.stopBroadcast();

    }
}