// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

interface IGroth16Verifier {
    function verifyProof(uint[2] calldata, uint[2][2] calldata, uint[2] calldata, uint[5] calldata)
        external view returns (bool);
}

/// @notice BENCHMARK ONLY. Runs the real Groth16Verifier (full pairing check,
/// same precompile gas) and then returns true, so a placeholder proof can be
/// settled through AgreementIndividual without a real snarkjs proof. Costs one
/// extra STATICCALL (~3k gas) over calling the real verifier directly. Do not
/// deploy outside a benchmark; with a real proof use Groth16Verifier directly.
contract BenchGroth16Verifier {
    IGroth16Verifier public immutable inner;
    constructor(address _inner) { inner = IGroth16Verifier(_inner); }

    function verifyProof(
        uint[2] calldata a, uint[2][2] calldata b, uint[2] calldata c, uint[5] calldata p
    ) external view returns (bool) {
        inner.verifyProof(a, b, c, p);
        return true;
    }
}
