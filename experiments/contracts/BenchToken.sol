// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice Benchmark-only payment token with an open mint.
contract BenchToken is ERC20 {
    constructor() ERC20("BenchToken", "BNCH") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}
