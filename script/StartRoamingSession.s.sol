// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Script.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "../src/Agreement.sol";
import {Vm} from "forge-std/Vm.sol";
import {console} from "forge-std/console.sol";

contract StartRoamingSession is Script {
    function run() external {
        Agreement agreement = Agreement(0x420a1bC7AA832E2521389c618bC7D309b8F4F1df);

        uint256 mno1PrivateKey = 0x0b926be7e0e254c513b112e6a82d89cd53f109fa5ad0b8be3b60ba436afda483;
        address mno1Address = 0x46CC7efbC0fb7F80c037B33c7fe416692Ea1075B;
        address ueAddress = 0x0Ea0Eb8061cBdaF6684852A583234d882dA63d25;
        uint256 estimatedCost = 1000000e6;

        uint256 nonce = agreement.nonces(mno1Address);

        bytes32 messageHash = keccak256(abi.encode(address(agreement), ueAddress, estimatedCost, nonce));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(mno1PrivateKey, messageHash);
        bytes memory signature = abi.encodePacked(r, s, v); 

        vm.startBroadcast(mno1PrivateKey);
        IERC20 paymentToken = agreement.paymentToken();
        paymentToken.approve(address(agreement), estimatedCost);
        uint sessionID = agreement.startRoamingSession(ueAddress, estimatedCost, nonce, signature);
        console.log("Session ID: ", sessionID);
        vm.stopBroadcast();

    }
}