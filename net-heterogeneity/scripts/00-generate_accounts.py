#!/usr/bin/env python3

"""
Generate funded Ethereum workload accounts.

Usage:

    python3 00-generate_accounts.py \
        200 \
        1000000000000000000000 \
        accounts.json

Output:

    [
        {
            "index": 0,
            "address": "0x...",
            "private_key": "0x...",
            "balance": "1000000000000000000000"
        },
        ...
    ]

The accounts are ordinary Ethereum accounts.
They are NOT QBFT validators.
"""

import json
import os
import sys

from web3 import Web3


def main():

    if len(sys.argv) != 4:
        print(
            "Usage: generate_accounts.py "
            "<num_accounts> <balance_wei> <output>"
        )
        sys.exit(1)

    num_accounts = int(sys.argv[1])
    balance = str(sys.argv[2])
    output = sys.argv[3]

    if num_accounts <= 0:
        raise SystemExit(
            "num_accounts must be > 0"
        )

    print(
        f"Generating {num_accounts} funded accounts..."
    )

    accounts = []

    for i in range(num_accounts):

        # Generate a cryptographically random Ethereum account.
        account = Web3().eth.account.create(
            extra_entropy=os.urandom(32).hex()
        )

        private_key = account.key.hex()

        accounts.append(
            {
                "index": i,
                "address": account.address,
                "private_key": private_key,
                "balance": balance,
            }
        )

    with open(output, "w") as f:

        json.dump(
            accounts,
            f,
            indent=2
        )

        f.write("\n")

    # Private-key file.
    os.chmod(output, 0o600)

    print()
    print(
        f"Generated {len(accounts)} accounts."
    )

    print(
        f"Saved to: {output}"
    )


if __name__ == "__main__":
    main()