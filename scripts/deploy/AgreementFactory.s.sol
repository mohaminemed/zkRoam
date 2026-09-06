// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "../../lib/forge-std/src/Script.sol";
import "../../contracts/AgreementFactory.sol";
import "../../contracts/Mock/MockUSDC.sol";
import "../../contracts/CDRVerifier.sol";
import {Vm} from "../../lib/forge-std/src/Vm.sol";
import {console} from "../../lib/forge-std/src/console.sol";

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