# CLI Command Reference

Use `--format json` for programmatic output and `--format table` for human-readable display.

## Onboarding (`caw onboard`)

| Command | Description |
|---------|-------------|
| `caw onboard --token <TOKEN> --create-wallet` | Full onboarding: pair with token and create wallet |
| `caw onboard self-test --wallet <wallet_uuid>` | Run blocked + allowed transfer probes to validate policy |
| `caw demo` | Run the quickstart demo workflow |

## Profile (`caw profile`)

| Command | Description |
|---------|-------------|
| `caw profile list` | List all agent profiles on this machine |
| `caw profile list --archived` | List archived (backed-up) profiles |
| `caw profile current` | Show the currently active profile and its credentials |
| `caw profile use <agent_id>` | Switch the default profile to a different agent |
| `caw profile env` | Show the environment of the active profile (sandbox, dev, prod) |
| `caw profile archive` | Archive the active profile (stop TSS, move to .bak) |
| `caw profile archive <agent_id>` | Archive a specific profile |
| `caw profile restore <agent_id>` | Restore an archived profile back to active |
| `caw profile claim` | Generate a claim link for an autonomous wallet (human opens link to claim ownership) |
| `caw profile claim-info` | Check the claim status of the current wallet (not_found, valid, expired, claimed) |

## Wallet (`caw wallet`)

| Command | Description |
|---------|-------------|
| `caw wallet list` | List wallets accessible to the caller |
| `caw wallet create --name <name> --main-node-id <id>` | Create a new MPC wallet |
| `caw wallet get <wallet_uuid>` | Get wallet details |
| `caw wallet balance <wallet_uuid>` | Get wallet token balances |
| `caw wallet status <wallet_uuid>` | Inspect spend summary and freeze state |
| `caw wallet update <wallet_uuid> --name <name>` | Update wallet properties |
| `caw wallet node-status` | Check TSS Node registration and connection status on Cobo side |

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
| `caw tx get <wallet_uuid> <tx_id>` | Get transaction details |
| `caw tx call <wallet_uuid> --chain <id> [--contract <addr> --calldata <hex>] [--value <amount>] [--instructions <json>] [--src-addr <addr>]` | Execute contract/program call (EVM: use --contract/--calldata; Solana: use --instructions). `--value` is in human-readable units (e.g. `0.1` for 0.1 ETH), not wei. Use --src-addr to specify source address. |
| `caw tx estimate-transfer-fee <wallet_uuid> --to <addr> --token <id> --amount <n> --chain <id>` | Estimate transfer fee |
| `caw tx estimate-call-fee <wallet_uuid> --contract <addr> --calldata <hex> --chain <id>` | Estimate contract call fee |

## Faucet (`caw faucet`)

| Command | Description |
|---------|-------------|
| `caw faucet tokens` | List available faucet tokens grouped by chain |
| `caw faucet deposit --address <addr> --token <token_id>` | Request test tokens for an address |

## TSS Node (`caw node`)

| Command | Description |
|---------|-------------|
| `caw node status` | Show TSS Node running status (pid, env, binary) |
| `caw node info` | Show TSS Node ID and metadata |
| `caw node health` | Run health checks: binary, db, keyfile, process, disk |
| `caw node start` | Start the TSS Node |
| `caw node stop` | Stop the TSS Node (checks for pending transactions first) |
| `caw node stop --force` | Stop the TSS Node even with pending transactions |
| `caw node restart` | Restart the TSS Node (stop + start) |
| `caw node logs` | View recent log lines (default 50) |
| `caw node logs -f` | Tail logs in real time |
| `caw node logs --lines 200` | View more log lines |

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
| `caw policy dry-run <wallet_id> --operation-type <type> --amount <n> --chain-id <id> [--token-id <id>] [--delegation-id <id>] [--dst-addr <addr>] [--contract-addr <addr>] [--calldata <hex>] [--principal-id <id>]` | Dry-run a policy evaluation |

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