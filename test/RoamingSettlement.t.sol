pragma solidity ^0.8.17;

import {Test} from "forge-std/Test.sol";
import {console} from "forge-std/console.sol";
import "../src/AgreementFactory.sol";
import "../src/Agreement.sol";
import "lib/openzeppelin-contracts/contracts/token/ERC20/ERC20.sol";
import "./Poseidon.sol";
import {Strings} from "lib/openzeppelin-contracts/contracts/utils/Strings.sol";

contract MockUSDC is ERC20 {
    constructor(string memory _name, string memory _symbol) ERC20(_name, _symbol) {
        _mint(address(this), type(uint256).max);
    }

    
    function decimals() public view override returns(uint8) {
        return 6;
    }
}

contract RoamingSettlementTest is Test {
    struct Rates {
        uint256 sms;
        uint256 voice;  
        uint256 data; 
    }

    MockUSDC public usdc;
    Groth16Verifier public groth16Verifier;
    AgreementFactory public factory;
    Agreement public agreement;
    uint privateKeyMNO1;
    uint privateKeyMNO2;
    address mno1;
    address mno2;
    address ue;
   

    function setUp() public {
        (mno1, privateKeyMNO1) = makeAddrAndKey("MNO1");
        (mno2, privateKeyMNO2) = makeAddrAndKey("MNO2");
        groth16Verifier= new Groth16Verifier();
        usdc = new MockUSDC("USDC", "USDC");
        factory = new AgreementFactory(address(usdc), address(groth16Verifier));
        ue = makeAddr("UE");
        deal(address(usdc), mno1, type(uint128).max);
        deal(address(usdc), mno2, type(uint128).max);
    }

    function testCreateRoamingAgreement() public {
        agreement = Agreement(createRoamingAgreement());
        (address sortedMno1, address sortedMno2) = _sortAddresses(mno1, mno2);
        assertEq(sortedMno1, agreement.mno1());
        assertEq(sortedMno2, agreement.mno2());
        assertEq(address(usdc), address(agreement.paymentToken()));
        assertEq(block.timestamp + 365 days, agreement.expiry());
    }

    function testCreateRoamingSession() public {
        agreement = Agreement(createRoamingAgreement());
        uint sessionID = createRoamingSession(agreement, mno1, privateKeyMNO1, 12000000e6);
        (address ueAddress, address hmno, uint estimatedCost, uint actualCost, uint startTime, uint endTime, bytes32 cdrHash, Agreement.SessionStatus status) 
            = agreement.sessions(sessionID);

        assertEq(ue, ueAddress);
        assertEq(mno1, hmno);
        assertEq(12000000e6, estimatedCost);
        assertEq(0, actualCost);
        assertEq(block.timestamp, startTime);
        assertEq(0, endTime);
        assertEq(bytes32(0), cdrHash);

    }

    function testSubmittingCDRbyUE() public {
        agreement = Agreement(createRoamingAgreement());
        uint sessionID = createRoamingSession(agreement, mno1, privateKeyMNO1, 12000000e6);
        uint256[3] memory roamingUsage = [uint256(10), uint256(10240), uint256(60)];
        uint256 cdrHash = PoseidonT4.hash(roamingUsage);
        vm.startPrank(ue);
        agreement.submitCDR(sessionID, bytes32(cdrHash));

    }

    function testSettlingRoamingSession() public {
        agreement = Agreement(createRoamingAgreement());
        uint sessionID = createRoamingSession(agreement, mno1, privateKeyMNO1, 12000000e6);
        vm.warp(block.timestamp + 7 days);
        // n_sms: 10
        // n_mb: 10240 
        // n_min: 60
        uint256[3] memory roamingUsage = [uint256(10), uint256(10240), uint256(60)];
        uint256 cdrHash = PoseidonT4.hash(roamingUsage);

        vm.startPrank(ue);
        agreement.submitCDR(sessionID, bytes32(cdrHash));

        uint256[2] memory a;
        a[0] = uint256(0x11a0df597d890ebb2e588ac6bfba02078ac074ee8ad81f77feb34dc954b00d18);
        a[1] = uint256(0x1fb8bd8136e118504767335412f96fdf0cf63b9d469b84a31237d021b27ad26a);

        uint256[2][2] memory b;
        b[0][0] = uint256(0x25811037df60e0fbce5c8cf5cae576205127786468212240e4209607d6f366a1);
        b[0][1] = uint256(0x244c2ae874a7734bc3b342af86032fd4c51de9f6018d3b7006330f55fc747e89);
        b[1][0] = uint256(0x20f82fe49897c4f2837d7c11ba69b143a6029e59dc95ae34b7cbda59cbd979bd);
        b[1][1] = uint256(0x16f04b36ebc70d12d6dc9278b37416b76760e2ecfe41280421feaec9703f45b6);

        uint256[2] memory c;
        c[0] = uint256(0x14929aadecb4cc91451ce9976c8f9c725547f81bead40e4b32ae7e1f23240c36);
        c[1] = uint256(0x09fce74ff52f64556f90c99bbf9e2a5998d1739456cd097e9a19f76f00fc90ae);

        // total is : 12e6(10 + 10240 + 60) = 123720e6
        uint totalCost = 123720e6;
        vm.startPrank(mno2);
        agreement.terminateRoamingSession(sessionID, a, b, c, totalCost);

    }

    function testCanNotCreateRoamingSessionWhenAgreementIsExpired() public {
        agreement = Agreement(createRoamingAgreement());
        vm.warp(block.timestamp + 365 days + 1 days);
        (address sortedMno1, address sortedMno2) = _sortAddresses(mno1, mno2);
        address _hmno = mno1;
        uint nonce = agreement.nonces(_hmno);
        bytes32 messageHash = keccak256(abi.encode(address(agreement), ue, uint256(12000000e6), nonce));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKeyMNO1, messageHash);
        bytes memory signature = abi.encodePacked(r, s, v);
        vm.startPrank(_hmno);
        usdc.approve(address(agreement), 12000000e6);
        vm.expectRevert();
        agreement.startRoamingSession(ue, 12000000e6, nonce, signature);

    }

    function testGroth16benchmarkGasForProofVerification() public {

        uint256[2] memory a;
        a[0] = uint256(18410970156618151414580785007392417495726517835055975375606164193550100243150); // pi_a[0]
        a[1] = uint256(2200184969053740846005047708725945799636503664267871457349147341609385637319); // pi_a[1]

        uint256[2][2] memory b;
        b[0][0] = uint256(8064533293671645220342571273559943181587018712913951230745527342641288060334); // pi_b[0][1]
        b[0][1] = uint256(15973863285094793595725303201008099317538847456173356973697849652833424699159); // pi_b[0][0]
        b[1][0] = uint256(613639259396839154750290679480500285166645257678394356226525889477644629069); // pi_b[1][1]
        b[1][1] = uint256(7287114959072187313245551246435107034121683057140269733241547921372487642315); // pi_b[1][0]

        uint256[2] memory c;
        c[0] = uint256(10959623725360317526270480587568751640421547057419262592530024505658297060654); // pi_c[0]
        c[1] = uint256(21772960376578326174149019251166556676716981283294112858807785137006639171545); // pi_c[1]

        uint256[5] memory publicSignals;
        publicSignals[0] = uint256(123720000000);
        publicSignals[1] = uint256(17439512958837443391502439933528566471468606623580200177475377690337083279807);
        publicSignals[2] = uint256(12000000);
        publicSignals[3] = uint256(12000000);
        publicSignals[4] = uint256(12000000);

        uint256 gasBefore = gasleft();
        bool result = groth16Verifier.verifyProof(a, b, c, publicSignals);
        assert(result);
        uint256 gasAfter = gasleft();
        console.log("Proof result: ", result);

        console.log("Gas used: ", gasBefore - gasAfter); // 220023 GAS
    }


    function createRoamingAgreement() internal returns(address){
        address[2] memory mnos = [mno1, mno2];
        (address sortedMno1, address sortedMno2) = _sortAddresses(mnos[0], mnos[1]);
        uint256[3] memory rates = [uint256(12e6), uint256(12e6), uint256(12e6)];
        uint expiry = block.timestamp + 365 days;

        bytes32 messageHashMno1 = keccak256(abi.encode(address(factory), sortedMno1, sortedMno2, rates, expiry, 1));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKeyMNO1, messageHashMno1);
        bytes memory signatureMno1 = abi.encodePacked(r, s, v);

        bytes32 messageHashMno2 = keccak256(abi.encode(address(factory), sortedMno1, sortedMno2, rates, expiry, 1));
        (v, r, s) = vm.sign(privateKeyMNO2, messageHashMno2);
        bytes memory signatureMno2 = abi.encodePacked(r, s, v);

        bytes[2] memory signatures = [signatureMno1, signatureMno2];

        return factory.createAgreement(mnos, rates, expiry, signatures);
    }

    function createRoamingSession(Agreement _agreement, address _hmno, uint pk, uint _estimatedCost) internal returns(uint) {
        uint nonce = _agreement.nonces(_hmno);
        bytes32 messageHash = keccak256(abi.encode(address(_agreement), ue, _estimatedCost, nonce));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, messageHash);
        bytes memory signature = abi.encodePacked(r, s, v);
        vm.startPrank(_hmno);
        usdc.approve(address(_agreement), _estimatedCost);
        return _agreement.startRoamingSession(ue, _estimatedCost, nonce, signature);

    }

    function _sortAddresses(address a, address b) 
        private pure returns (address, address) 
    {
        return a < b ? (a, b) : (b, a);
    }

}