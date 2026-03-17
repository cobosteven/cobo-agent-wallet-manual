---
name: cobo-agentic-wallet-sandbox
description: |
  Cobo Agentic Wallet skill for the **sandbox** environment (`--env sandbox`, API: api-agent-wallet-core.sandbox.cobo.com).
  Use for agent wallet use cases and agent-wallet-related guidance — including Cobo Agentic Wallet operations via the `caw` CLI or Cobo API: wallet onboarding and setup, transfers of stablecoins (USDC, USDT, DAI) and tokens (ETH, WETH, WBTC, SOL, ARB, OP, MATIC), smart contract calls, balance and transaction queries, and policy denial handling.
  Covers DeFi execution through Cobo's MPC/TSS policy layer on EVM chains (Base, Ethereum, Arbitrum, Optimism, Polygon) and Solana (mainnet via Jupiter, devnet): Aave V3 borrow/repay, Uniswap V3 swaps, DCA strategies, grid trading, and prediction market positions.
  Use when: user mentions caw, cobo-agentic-wallet, MPC wallet, TSS node, Cobo Portal; needs smart contract or DeFi protocol access through a policy-enforced agent wallet; or asks about agent wallets, policy-enforced wallets, or how agents manage crypto in general — and the target environment is sandbox.
  NOT for: fiat currency operations — credit card payments, bank wire transfers, or crypto-to-fiat off-ramp flows.
---

# Cobo Agentic Wallet (Sandbox)

Cobo Agentic Wallet is a policy-enforced crypto wallet for AI agents. Owners set spending limits and approve transactions; agents operate within those guardrails. The `caw` CLI is the primary interface.

## Install

```bash
pip install --upgrade cobo-agentic-wallet  # always update to latest before use
caw --help
```

## Environment

| Environment | `--env` value | API URL                                          | Web Console |
|-------------|---------------|--------------------------------------------------|-------------|
| Sandbox | `sandbox` | `https://api-agent-wallet-core.sandbox.cobo.com` | https://agenticwallet.sandbox.cobo.com/ |

Set the API URL before any command:

```bash
export AGENT_WALLET_API_URL=https://api-agent-wallet-core.sandbox.cobo.com
```

## Onboarding

If the user provides a **token** → Supervised path. Otherwise → Autonomous path (default).

### Autonomous Path (default — no token)

```bash
caw onboard --create-wallet --env sandbox
```

> See [Error Handling — Onboarding errors](./recipes/error-handling.md#onboarding-errors) for onboarding errors.

This single command completes the full setup. **Do NOT create addresses or run self-test after this** — the wallet is ready to use immediately. Only proceed with further steps if the user explicitly requests them.

Notes:
- **`--sponsor false` is required** for transfers — autonomous wallets do NOT support gas sponsorship.

### Supervised Path (token provided)

Human initiates from Web Console, provides a setup token. Agent pairs + creates a wallet under owner's policy supervision.

**Step 1 — Run onboard**

```bash
caw onboard --token <TOKEN> --create-wallet --env sandbox
```

Runs 5 sequential steps (~60–180s):
`[1/5]` Register → `[2/5]` Download TSS → `[3/5]` Init TSS → `[4/5]` Start TSS → `[5/5]` Create wallet.

> **IMPORTANT:** Long-running command. Run in background, poll output every 10–15s, report each `[n/5]` step to the user.

**Step 2 — Save API key**

```bash
export AGENT_WALLET_API_KEY=<api_key_from_output>
```

**Step 3 — Verify profile**

```bash
caw profile current
```

Confirm `agent_id`, `wallet_uuid`, `env`, and `onboarded_at` are populated.

**Step 4 — Create an address**

```bash
caw --format json address create --chain SOL
```

> `--chain` accepts user-friendly chain IDs (e.g., `SOL`, `ETH`). The CLI resolves them to internal identifiers automatically.

**Step 5 — Validate with self-test**

```bash
caw --format json onboard self-test
```

**Step 6 — Report to user**

Print a summary with: `agent_id`, `wallet_uuid`, address(es), `env`, and config paths.

---

### Claiming — Transfer Ownership to a Human

When the user wants to claim a wallet (e.g., "我要 claim 这个钱包", "claim the wallet"), use these commands:

```bash
caw profile claim                   # generate a claim link
caw profile claim-info              # check claim status
```

`claim` returns a `claim_link` URL. Share this link with the human — they open it in the Web Console to complete the ownership transfer. Once claimed, the wallet switches to Supervised mode (delegation is created, `--sponsor true` becomes available).

Use `claim-info` to check the current state: `not_found` (no claim initiated), `valid` (pending, waiting for human), `expired`, or `claimed` (transfer complete).

---

### Profile

Each `caw onboard` creates a separate **profile** — an isolated identity with its own credentials, wallet, and TSS Node files. Multiple profiles can coexist on one machine.

- **Default profile**: Most commands automatically use the active profile. Switch it with `caw profile use <agent_id>`.
- **`--profile` flag**: Any command accepts `--profile <agent_id>` to target a specific profile without switching the default. Use this when running multiple agents concurrently.

```bash
# Example: transfer using a non-default profile
caw --profile caw_agent_abc123 tx transfer --to 0x... --token SOLDEV_SOL_USDC --amount 0.0001 --chain SOLDEV_SOL
```

See `caw profile --help` for all profile subcommands (`list`, `current`, `use`, `env`, `archive`, `restore`).

> **ONLY use archive when a previous onboarding has failed and you need to retry.** Do NOT archive before a fresh onboarding — the `onboard` command creates a new profile automatically.

---

## Key Notes

- **`--format json`** for programmatic output; `--format table` only when displaying to the user.
- **`--sponsor` option**: `--sponsor true` (default) uses Cobo Gasless. Autonomous mode wallets must use `--sponsor false`.
- **Long-running commands** (`caw onboard --create-wallet`): run in background, poll output every 10–15s, report each `[n/total]` progress step.
- **TSS Node auto-start**: `caw tx transfer` and `caw tx call` automatically check TSS Node status and start it if offline. `caw node stop` checks for pending transactions — use `--force` to skip.
- **wallet_uuid is optional** in most commands — if omitted, the CLI uses the active profile's wallet.
- **StandardResponse format** — API responses are wrapped as `{ success: true, result: <data> }`. Extract from `result` first.
- **Non-zero exit codes** indicate failure — check stdout/stderr before retrying.

## Reference

Read the file that matches the user's task. Do not load files that aren't relevant.

- [Recipes](./recipes.md) — Read for quick one-liner examples of common operations
  - [Policy Management](./recipes/policy-management.md)
  - [Error Handling](./recipes/error-handling.md)

**DeFi recipes** — read the matching file when the user asks about a specific strategy:

| User asks about… | Read |
|---|---|
| Aave, borrow, repay, supply, collateral | [evm-defi-aave.md](./recipes/evm-defi-aave.md) |
| DEX swap, Uniswap, token exchange (EVM) | [evm-defi-dex-swap.md](./recipes/evm-defi-dex-swap.md) |
| DCA, dollar cost average, recurring buy (EVM) | [evm-defi-dca.md](./recipes/evm-defi-dca.md) |
| Grid trading, ladder orders (EVM) | [evm-defi-grid-trading.md](./recipes/evm-defi-grid-trading.md) |
| Solana DEX swap, Jupiter, SOL/USDC | [solana-defi-dex-swap.md](./recipes/solana-defi-dex-swap.md) |
| Solana DCA, recurring SOL purchase | [solana-defi-dca.md](./recipes/solana-defi-dca.md) |
| Solana grid trading | [solana-defi-grid-trading.md](./recipes/solana-defi-grid-trading.md) |
| Prediction market, Drift, Polymarket, long/short | [solana-defi-prediction-market.md](./recipes/solana-defi-prediction-market.md) |
| Policy denial, 403 error, TRANSFER_LIMIT_EXCEEDED | [error-handling.md](./recipes/error-handling.md) |
| Policy setup, dry-run, delegation | [policy-management.md](./recipes/policy-management.md) |
