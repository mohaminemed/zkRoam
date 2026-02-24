// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Script.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "../src/Agreement.sol";
import {Vm} from "forge-std/Vm.sol";
import {console} from "forge-std/console.sol";
import "../test/Poseidon.sol";
import {console} from "forge-std/console.sol";
contract SubmitCDR is Script {
    function run() external {
        Agreement agreement = Agreement(0x420a1bC7AA832E2521389c618bC7D309b8F4F1df);

        address ueAddress = 0x0Ea0Eb8061cBdaF6684852A583234d882dA63d25;
        uint256 uePrivateKey = 0x309df18e90f222e91c2955fba1110f09a2e039aa9d899312447f3d6ef7d54e86;
        vm.startBroadcast(uePrivateKey);
        
        uint sessionId = agreement.getLatestRoamingSession(ueAddress);

        uint256[3] memory roamingUsage = [uint256(10), uint256(10240), uint256(60)];
        uint256 cdrHash = PoseidonT4.hash(roamingUsage);

        console.log("CDR HASH: ", cdrHash);
        
        agreement.submitCDR(sessionId, bytes32(cdrHash));
        vm.stopBroadcast();

    }
}