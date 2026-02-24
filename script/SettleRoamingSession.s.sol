// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Script.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "../src/Agreement.sol";
import {Vm} from "forge-std/Vm.sol";
import {console} from "forge-std/console.sol";
import "../test/Poseidon.sol";

contract SettleRoamingSession is Script {
    function run() external {
        Agreement agreement = Agreement(0x420a1bC7AA832E2521389c618bC7D309b8F4F1df);
        uint lastSession = agreement.sessionCounter();

        uint256 mno2PrivateKey = 0x8b506681f432e9d3765c7452a37b063010939cc388784fee99a896e827338f32;
        
        uint256[2] memory a;
        a[0] = uint256(14728542849133373373433668966213365112701723801799448447598276716099314107380); // pi_a[0]
        a[1] = uint256(10535248145403870161099822780108389671032706523746119807941992374564056084043); // pi_a[1]

        uint256[2][2] memory b;
        b[0][0] = uint256(19515139809714802826412629220563427797882457637646860493090549454405647489639); // pi_b[0][1]
        b[0][1] = uint256(11703363424749178331446711541824871396473049980998013336565221512834081583152); // pi_b[0][0]
        b[1][0] = uint256(13774103598048977064982792998874591920233425035231763365307880223619534091303); // pi_b[1][1]
        b[1][1] = uint256(11141798007757290899395264163637299010476660923072616320791566341134712066173); // pi_b[1][0]

        uint256[2] memory c;
        c[0] = uint256(2019392710110452899749729598940966586629600365204748187027222972916044503440); // pi_c[0]
        c[1] = uint256(17686001427449836129241636346206509806098002726866285141744610756257887943525); // pi_c[1]

        // total is : 12e6(10 + 10240 + 60) = 123720e6
        uint totalCost = 123720e6;

        vm.startBroadcast(mno2PrivateKey);
        agreement.terminateRoamingSession(lastSession, a, b, c, totalCost);
        vm.stopBroadcast();

    }
}