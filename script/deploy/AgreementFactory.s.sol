// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Script.sol";
import "../../src/AgreementFactory.sol";
import "../../src/Mock/MockUSDC.sol";
import "../../src/Groth16Verifier.sol";
import {Vm} from "forge-std/Vm.sol";
import {console} from "forge-std/console.sol";

contract AgreementFactoryDeployment is Script {
    function run() external {
        uint256 deployerPrivateKey = 0x359df918ad082e59497b21bc1b080b66114d829f1add1ede3a739079992c28bc;
        vm.startBroadcast(deployerPrivateKey);
        MockUSDC usdc = new MockUSDC();
        Groth16Verifier groth16Verifier = new Groth16Verifier();
        AgreementFactory agreementFactory = new AgreementFactory(address(usdc), address(groth16Verifier));
        console.log("USDC contract: ", address(usdc));
        console.log("contract address: ", address(agreementFactory));
        vm.stopBroadcast();
    }
}