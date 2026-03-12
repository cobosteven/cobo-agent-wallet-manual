# CLI Command Reference

Use `--format json` for programmatic output and `--format table` for human-readable display.

## Onboarding (`caw onboard`)

| Command | Description |
|---------|-------------|
| `caw onboard provision --token <TOKEN>` | Provision with owner-issued setup token |
| `caw onboard self-test --wallet <wallet_uuid>` | Run blocked + allowed transfer probes to validate policy |
| `caw demo` | Run the quickstart demo workflow |

## Wallet (`caw wallet`)

| Command | Description |
|---------|-------------|
| `caw wallet list` | List wallets accessible to the caller |
| `caw wallet create --name <name> --main-node-id <id>` | Create a new MPC wallet |
| `caw wallet get <wallet_uuid>` | Get wallet details |
| `caw wallet balance <wallet_uuid>` | Get wallet token balances |
| `caw wallet status <wallet_uuid>` | Inspect spend summary and freeze state |
| `caw wallet update <wallet_uuid> --name <name>` | Update wallet properties |

## Address (`caw address`)

| Command | Description |
|---------|-------------|
| `caw address list <wallet_uuid>` | List addresses for a wallet |
| `caw address create <wallet_uuid> --chain <id>` | Create new address on chain |
| `caw address recent <wallet_uuid>` | List recently used addresses |

## Transactions (`caw tx`)

| Command | Description |
|---------|-------------|
| `caw tx transfer <wallet_uuid> --to <addr> --token <id> --amount <n> --chain <id>` | Transfer tokens with policy enforcement |
| `caw tx list <wallet_uuid>` | List recent transactions with status |
| `caw tx get <tx_id>` | Get transaction details |
| `caw tx call <wallet_uuid> --contract <addr> --calldata <hex> --chain <id>` | Execute contract call |
| `caw tx estimate-transfer-fee <wallet_uuid> --to <addr> --token <id> --amount <n> --chain <id>` | Estimate transfer fee |
| `caw tx estimate-call-fee <wallet_uuid> --contract <addr> --calldata <hex> --chain <id>` | Estimate contract call fee |

## Faucet (`caw faucet`)

| Command | Description |
|---------|-------------|
| `caw faucet tokens` | List available faucet tokens grouped by chain |
| `caw faucet deposit --address <addr> --token <token_id>` | Request test tokens for an address |

## Delegation (`caw delegation`)

| Command | Description |
|---------|-------------|
| `caw delegation get <delegation_id>` | Get delegation details |
| `caw delegation received` | List delegations received as operator |

## Policy (`caw policy`)

| Command | Description |
|---------|-------------|
| `caw policy list --scope <scope>` | List policies (scope: global, wallet, delegation) |
| `caw policy get <policy_id>` | Get policy details |
| `caw policy dry-run <policy_id> --action <action> --params <json>` | Test policy evaluation |

## Pending Operations (`caw pending`)

| Command | Description |
|---------|-------------|
| `caw pending list` | List pending operations |
| `caw pending get <operation_id>` | Get pending operation details |

## Metadata (`caw meta`)

| Command | Description |
|---------|-------------|
| `caw meta chains` | List supported chains |
| `caw meta tokens` | List supported tokens/assets |
| `caw meta prices <asset_coins>` | Get asset coin prices |