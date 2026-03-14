# Solana DeFi — Prediction Market

Stake tokens on price prediction outcomes on Solana.

## Overview

| Environment | Chain ID | Approach |
|-------------|----------|----------|
| Devnet | `SOLDEV_SOL` | Memo + System Transfer (labeled stake simulation) |
| Mainnet | `SOL` | Drift Protocol perpetuals (LONG/SHORT position) |

Use `caw tx call` to submit Solana program instructions.

---

## Prerequisites

**Tools**
- `caw` CLI installed and configured (`caw onboard` complete)
- `python3` — for base64 encoding of instruction data
- `bc` — for lamport conversion
- Mainnet (Drift): `pip install driftpy aiohttp anchorpy solders` and a Solana RPC endpoint

**Wallet state**
- Devnet: SOL balance on `SOLDEV_SOL` sufficient for stake amounts plus fees (fund via `caw faucet deposit`)
- Mainnet (Drift): funded Drift account with USDC collateral deposited; SOL for transaction fees
- Mainnet (Polymarket/Polygon): USDC balance on `MATIC`

**One-time setup**
- Devnet: provide a `DEST_ADDR` (any valid Solana address to receive the simulated stake transfer)
- Mainnet (Drift): set `SOLANA_RPC_URL` environment variable; ensure Drift user account exists
- Mainnet (Polymarket): install `py-clob-client` SDK and configure API credentials

**Gas**
- Solana: transaction fees paid in SOL (~0.001 SOL per transaction)
- Polygon: transaction fees paid in MATIC (or sponsored via Cobo Gasless)

---

## Option A — Devnet (simulation)

Each prediction is a Memo-labeled SOL transfer that records the position on-chain.

### Prediction market simulation script

```bash
#!/bin/bash
# prediction_devnet.sh - Prediction market simulation on devnet

WALLET_UUID="<wallet_uuid>"
WALLET_ADDR="<wallet_addr>"
DEST_ADDR="<destination_address>"
CHAIN="SOLDEV_SOL"

MEMO_PROG="MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
SYS_PROG="11111111111111111111111111111111"

# Prediction positions: SIDE TARGET_LABEL STAKE_LAMPORTS
PREDICTIONS=(
  "LONG SOL_USD_TARGET_200USD 5000000"
  "SHORT SOL_USD_TARGET_150USD 5000000"
)

encode_transfer_data() {
  python3 -c "import struct, base64; print(base64.b64encode(struct.pack('<I', 2) + struct.pack('<Q', $1)).decode())"
}

echo "=== Prediction Market (devnet) ==="

for entry in "${PREDICTIONS[@]}"; do
  read -r SIDE TARGET_LABEL LAMPORTS <<< "$entry"
  
  LABEL="PREDICTION_${SIDE}_${TARGET_LABEL}"
  MEMO_DATA=$(echo -n "$LABEL" | base64)
  TRANSFER_DATA=$(encode_transfer_data $LAMPORTS)
  
  SOL_AMOUNT=$(echo "scale=4; $LAMPORTS / 1000000000" | bc)
  echo ""
  echo "[$SIDE] stake=$SOL_AMOUNT SOL target=$TARGET_LABEL..."
  
  INSTRUCTIONS=$(cat <<EOF
[
  {
    "program_id": "$MEMO_PROG",
    "accounts": [{"pubkey": "$WALLET_ADDR", "is_signer": true, "is_writable": false}],
    "data": "$MEMO_DATA"
  },
  {
    "program_id": "$SYS_PROG",
    "accounts": [
      {"pubkey": "$WALLET_ADDR", "is_signer": true, "is_writable": true},
      {"pubkey": "$DEST_ADDR", "is_writable": true}
    ],
    "data": "$TRANSFER_DATA"
  }
]
EOF
)
  
  caw tx call "$WALLET_UUID" \
    --instructions "$INSTRUCTIONS" \
    --chain "$CHAIN" \
    --src-addr "$WALLET_ADDR"
done

echo ""
echo "All positions submitted."
```

### Single prediction execution

```bash
# LONG position on SOL reaching $200
caw tx call <wallet_uuid> \
  --instructions '[{"program_id": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "accounts": [{"pubkey": "<WALLET_ADDR>", "is_signer": true, "is_writable": false}], "data": "UFJFRINUTFJJT05fTE9OR19TT0xfVVNEX1RBUkdFVF8yMDBVU0Q="}, {"program_id": "11111111111111111111111111111111", "accounts": [{"pubkey": "<WALLET_ADDR>", "is_signer": true, "is_writable": true}, {"pubkey": "<DEST_ADDR>", "is_writable": true}], "data": "AgAAAABwTEsAAAA="}]' \
  --chain SOLDEV_SOL \
  --src-addr <WALLET_ADDR>

# Check status
caw tx get <wallet_uuid> <tx_id>
```

---

## Option B — Mainnet (Drift Protocol perpetuals)

> **Note**: This section provides a reference framework for Drift Protocol integration. Full implementation requires:
> - A Solana RPC endpoint (e.g., Helius, QuickNode)
> - The `driftpy` Python library with proper setup
> - A funded Drift account with USDC collateral
>
> The code below shows the pattern; you must complete the RPC configuration for your environment.

Drift Protocol (`dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH`) supports LONG/SHORT perpetual positions on SOL-PERP.

### Prerequisites

1. Install `driftpy` to build instructions:
   ```bash
   pip install driftpy aiohttp anchorpy solders
   ```

2. Set up Solana RPC endpoint (required):
   ```bash
   export SOLANA_RPC_URL="https://api.mainnet-beta.solana.com"  # or your RPC provider
   ```

### Building Drift instructions

Create a helper script to generate Drift order instructions:

```python
#!/usr/bin/env python3
# generate_drift_ix.py - Generate Drift perp order instructions
#
# Usage: python3 generate_drift_ix.py <SIDE> <SIZE_USD> <WALLET_ADDR>
# Example: python3 generate_drift_ix.py LONG 10 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU
#
# IMPORTANT: This script requires:
# 1. A valid SOLANA_RPC_URL environment variable
# 2. An existing Drift account with USDC collateral
# 3. pip install driftpy aiohttp anchorpy solders

import asyncio
import json
import base64
import os
import sys

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from driftpy.drift_client import DriftClient
from driftpy.types import PositionDirection, OrderType, OrderParams, MarketType, PostOnlyParams
from driftpy.constants.numeric_constants import BASE_PRECISION
from anchorpy import Provider, Wallet

SOL_PERP_MARKET_INDEX = 0

def to_cli_instruction(ix):
    """Convert solders Instruction to caw CLI format."""
    accounts = [
        {
            "pubkey": str(meta.pubkey),
            "is_signer": meta.is_signer,
            "is_writable": meta.is_writable,
        }
        for meta in ix.accounts
    ]
    return {
        "program_id": str(ix.program_id),
        "accounts": accounts,
        "data": base64.b64encode(bytes(ix.data)).decode(),
    }

async def main():
    if len(sys.argv) < 4:
        print("Usage: python3 generate_drift_ix.py <SIDE> <SIZE_USD> <WALLET_ADDR>", file=sys.stderr)
        sys.exit(1)
    
    side = sys.argv[1].upper()
    size_usd = float(sys.argv[2])
    wallet_addr = sys.argv[3]
    
    rpc_url = os.environ.get("SOLANA_RPC_URL")
    if not rpc_url:
        print("Error: SOLANA_RPC_URL environment variable not set", file=sys.stderr)
        sys.exit(1)
    
    # Connect to Solana
    connection = AsyncClient(rpc_url)
    
    # Create a dummy wallet for read-only operations
    # The actual signing is done by Cobo's TSS
    dummy_wallet = Wallet.local()
    provider = Provider(connection, dummy_wallet)
    
    # Initialize Drift client
    drift_client = DriftClient(provider, authority=Pubkey.from_string(wallet_addr))
    await drift_client.subscribe()
    
    try:
        direction = PositionDirection.Long() if side == "LONG" else PositionDirection.Short()
        
        order_params = OrderParams(
            order_type=OrderType.Market(),
            market_type=MarketType.Perp(),
            direction=direction,
            market_index=SOL_PERP_MARKET_INDEX,
            base_asset_amount=int(size_usd * BASE_PRECISION / 100),
            price=0,
            reduce_only=False,
            post_only=PostOnlyParams.None_(),
        )
        
        # Get instruction from driftpy
        ix = await drift_client.get_place_perp_order_ix(order_params)
        
        # Output in caw CLI format
        print(json.dumps([to_cli_instruction(ix)]))
    
    finally:
        await drift_client.unsubscribe()
        await connection.close()

if __name__ == "__main__":
    asyncio.run(main())
```

### Execute Drift position

```bash
#!/bin/bash
# drift_position.sh - Open Drift perpetual position

WALLET_UUID="<wallet_uuid>"
WALLET_ADDR="<wallet_addr>"
CHAIN="SOL"

SIDE="${1:-LONG}"
SIZE_USD="${2:-10}"

echo "Opening $SIDE position, ~\$$SIZE_USD notional..."

# Generate instructions using driftpy
INSTRUCTIONS=$(python3 generate_drift_ix.py "$SIDE" "$SIZE_USD" "$WALLET_ADDR")

if [ -z "$INSTRUCTIONS" ] || [ "$INSTRUCTIONS" == "null" ]; then
  echo "Error: Failed to generate Drift instructions"
  exit 1
fi

caw tx call "$WALLET_UUID" \
  --instructions "$INSTRUCTIONS" \
  --chain "$CHAIN" \
  --src-addr "$WALLET_ADDR"

echo "Position submitted."
```

---

## Alternative: Polymarket (via EVM on Polygon)

If on-chain Solana instruction complexity is a barrier, Polymarket provides a REST API for prediction market positions on Polygon (`MATIC`). Since Polymarket runs on Polygon (EVM), you can use `caw tx call` with `--contract` and `--calldata` for EVM contract interactions.

```bash
# List prediction markets
curl -s "https://clob.polymarket.com/markets?search=SOL" | jq '.[] | {id, question, outcome_prices}'

# For trading, use the py-clob-client Python SDK:
# pip install py-clob-client
```


---

## Notes

- **Drift Protocol**: Full integration requires a Solana RPC endpoint and the `driftpy` library. The pattern above shows how to convert `driftpy` instructions to CLI-compatible format for Cobo signing.
- **Polymarket**: Easier REST-based API; runs on Polygon (`MATIC`). Good alternative when Solana mainnet perp setup is complex.
- **Status lifecycle**: `Submitted → PendingScreening → Broadcasting → Confirming → Completed`
- **Position management**: Use driftpy to also generate close position / reduce only instructions.
