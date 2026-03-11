---
name: cobo-agentic-wallet
description: |
  Use this skill for any crypto wallet operation: sending tokens, calling contracts, checking balances, querying transactions, or handling policy denials.
---

# cobo-agentic-wallet

Operate wallets through the `caw` CLI with policy enforcement. Owners configure limits and approve transactions via Telegram; agents execute within those guardrails.

## Install

```bash
pip install cobo-agentic-wallet
caw --help
```

## Auth Setup

Credentials are auto-stored by `caw onboard provision` / `caw onboard init`. Set API URL before running any command:

```bash
export AGENT_WALLET_API_URL=https://api-agent-wallet-core.sandbox.cobo.com
export AGENT_WALLET_API_KEY=<your_api_key>  # only needed if not already stored
```

## Getting Started

### Path A: Owner-First (Telegram → Token → Provision)

1. Owner opens Telegram bot and sends `/start`.
2. Owner authenticates via magic link.
3. Owner configures chains, budgets, and limits in the chat.
4. Owner receives a setup token.
5. Agent runs:
   ```bash
   caw --format table onboard provision --token <TOKEN>
   ```
6. Wallet is auto-provisioned with key shares, delegation, and policies.

### Path B: Agent-First (Init → Share Link → Auto-Detect)

1. Agent runs:
   ```bash
   caw --format table onboard init
   ```
2. Agent shares the returned deep link with the owner.
3. Owner opens the link, authenticates, and configures preferences.
4. The command detects activation automatically, then downloads + init + starts TSS-Node, creates vault, and runs KeyGen.
5. The command exits when onboarding is complete.

`caw onboard init` and `caw onboard provision` are long-running blocking commands (typically 60–180 seconds). Do not interrupt them.

### Execution guidance for AI agents

Before running any `caw` command, ensure `AGENT_WALLET_API_URL` is set.

Both `caw onboard init` and `caw onboard provision` run for 60–180 seconds. When executing:

1. **Run in background** and poll output every 10–15 seconds.
2. **Report progress** to the user as each step completes (don't wait for the user to ask).
3. **Share the deep link immediately** — for `caw onboard init` (Path B), the Telegram deep link is printed early in the output. Surface it to the user right away so the owner can start the Telegram flow in parallel.

**Progress steps** (printed to stdout):
```
[1/5] Registering with Cobo backend... done
[2/5] Downloading TSS-Node in background...
      Share this link with your wallet owner:
        https://t.me/<bot>?start=<agent_id>
[3/5] Waiting for owner to connect... done
[4/5] Initializing TSS-Node... done
[5/5] Starting TSS-Node and creating wallet... done
```

**Successful completion** — a table is printed at the end:
```
field                 | value
----------------------+------------------------------------------
agent_id              | caw_agent_<hex>
status                | active
deep_link             | https://t.me/<bot>?start=caw_agent_<hex>
wallet_uuid           | <uuid>
onboarding_session_id | <uuid>
config_path           | ~/.cobo-agent-wallet/config
tss_node_path         | ~/.cobo-agent-wallet/tss-node
```

Exit code 0 = success. Any non-zero exit code indicates failure — check output for error details.

## CLI Commands

All commands use `caw` as the entry point with subcommand groups. Use `--format json` when you need to parse the output programmatically (transfers, policy checks, audit queries). Use `--format table` for human-readable display (onboarding, status checks).

### Onboarding (`caw onboard`)

| Command | Description |
|---------|-------------|
| `caw onboard provision --token <TOKEN>` | Path A: provision with owner-issued setup token |
| `caw onboard init` | Path B: agent-first onboarding, returns deep link for owner |
| `caw onboard self-test --wallet <wallet_uuid>` | Run blocked + allowed transfer probes to validate policy |
| `caw demo` | Run the quickstart demo workflow |

### Wallet (`caw wallet`)

| Command | Description |
|---------|-------------|
| `caw wallet list` | List wallets accessible to the caller |
| `caw wallet create --name <name> --main-node-id <id>` | Create a new MPC wallet |
| `caw wallet get <wallet_uuid>` | Get wallet details |
| `caw wallet balance <wallet_uuid>` | Get wallet token balances |
| `caw wallet status <wallet_uuid>` | Inspect spend summary and freeze state |
| `caw wallet update <wallet_uuid> --name <name>` | Update wallet properties |
| `caw wallet archive <wallet_uuid>` | Archive a wallet |
| `caw wallet node-status <wallet_uuid>` | Get TSS node status |

### Address (`caw address`)

| Command | Description |
|---------|-------------|
| `caw address list <wallet_uuid>` | List addresses for a wallet |
| `caw address create <wallet_uuid> --chain <id>` | Create new address on chain |
| `caw address recent <wallet_uuid>` | List recently used addresses |

### Transactions (`caw tx`)

| Command | Description |
|---------|-------------|
| `caw tx transfer <wallet_uuid> --to <addr> --token <id> --amount <n> --chain <id>` | Transfer tokens with policy enforcement |
| `caw tx list <wallet_uuid>` | List recent transactions with status |
| `caw tx get <tx_id>` | Get transaction details |
| `caw tx call <wallet_uuid> --contract <addr> --calldata <hex> --chain <id>` | Execute contract call |
| `caw tx estimate-transfer-fee <wallet_uuid> --to <addr> --token <id> --amount <n> --chain <id>` | Estimate transfer fee |
| `caw tx estimate-call-fee <wallet_uuid> --contract <addr> --calldata <hex> --chain <id>` | Estimate contract call fee |

### Delegation (`caw delegation`)

| Command | Description |
|---------|-------------|
| `caw delegation create <wallet_uuid> --to <operator_id> --max-tx <amount>` | Create delegation with operator |
| `caw delegation list` | List delegations created by owner |
| `caw delegation get <delegation_id>` | Get delegation details |
| `caw delegation update <delegation_id> --max-tx <amount>` | Update delegation |
| `caw delegation revoke <delegation_id>` | Revoke a delegation |
| `caw delegation received` | List delegations received as operator |
| `caw delegation freeze --scope <owner\|wallet\|delegation> [--wallet-id <id>] [--delegation-id <id>]` | Freeze delegations |
| `caw delegation unfreeze --scope <owner\|wallet\|delegation> [--wallet-id <id>] [--delegation-id <id>]` | Unfreeze delegations |

### Policy (`caw policy`)

| Command | Description |
|---------|-------------|
| `caw policy list` | List policies |
| `caw policy get <policy_id>` | Get policy details |
| `caw policy create --delegation-id <id> --name <name> --type <type> --rules <json>` | Create policy |
| `caw policy update <policy_id> --rules <json>` | Update policy |
| `caw policy deactivate <policy_id>` | Deactivate policy |
| `caw policy dry-run <policy_id> --action <action> --params <json>` | Test policy evaluation |

### Pending Operations (`caw pending`)

| Command | Description |
|---------|-------------|
| `caw pending list` | List pending operations |
| `caw pending get <operation_id>` | Get pending operation details |
| `caw pending approve <operation_id>` | Approve pending operation |
| `caw pending reject <operation_id>` | Reject pending operation |

### Agent (`caw agent`)

| Command | Description |
|---------|-------------|
| `caw agent status` | Get agent status |
| `caw agent list` | List agents owned by principal |
| `caw agent api-key create --name <name>` | Create API key |
| `caw agent api-key list` | List API keys |
| `caw agent api-key revoke <api_key_id>` | Revoke API key |

### Audit (`caw audit`)

| Command | Description |
|---------|-------------|
| `caw audit logs [--wallet <uuid>] [--result allowed\|denied\|error] [--action <type>]` | Query audit logs with filters |

## Owner-Side NL Assistant

Owners interact via Telegram:

- `/start` — Begin onboarding or resume session
- Natural language commands: set budgets, freeze/unfreeze, approve/reject, view activity
- Push notifications for approvals, policy violations, deposits, and transfers

## Usage Examples

### Path B onboarding (sandbox)
```bash
export AGENT_WALLET_API_URL=https://api.agent-wallet-core.sandbox.cobo.com
caw --format table onboard init --tss-env sandbox
```

### Path A onboarding (sandbox)
```bash
export AGENT_WALLET_API_URL=https://api.agent-wallet-core.sandbox.cobo.com
caw --format table onboard provision --token <TOKEN> --tss-env sandbox
```

### Self-test after provisioning
```bash
caw --format json onboard self-test --wallet <wallet_uuid>
```

### Transfer tokens
```bash
caw --format json tx transfer <wallet_uuid> \
  --to 0x1234...abcd \
  --token USDC \
  --amount 10 \
  --chain BASE \
  --request-id pay-invoice-1001
```

### List recent transactions
```bash
caw --format json tx list <wallet_uuid> --limit 20
```

### Check wallet balance
```bash
caw --format json wallet balance <wallet_uuid>
```

### Freeze all delegations for a wallet
```bash
caw delegation freeze --scope wallet --wallet-id <wallet_uuid> --yes
```

### Query audit logs
```bash
caw --format json audit logs --wallet <wallet_uuid> --result denied --limit 50
```

## Error Handling

- Policy denials return structured JSON in `--format json` mode.
- Use denial `suggestion` field to retry with adjusted parameters.
- `onboard self-test` validates both blocked and allowed transfers automatically.
