pragma solidity ^0.8.19;
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";


contract MockUSDC is ERC20 {
    constructor() ERC20("USD Coin", "USDC") {
        _mint(0x73a7245EFcAeb3Addf55a55afFc75A956b69854c, 1000000000000000);
        _mint(0x0Ea0Eb8061cBdaF6684852A583234d882dA63d25, 1000000000000000);
        _mint(0x58D85998a7c6ed077f9FB913700f5f5Da539a786, 1000000000000000);
        _mint(0x46CC7efbC0fb7F80c037B33c7fe416692Ea1075B, 1000000000000000);
    }

    function decimals() public view override returns(uint8) {
        return 6;
    }

    function drop(uint256 _amount) external {
        _mint(msg.sender, _amount);
    }
}