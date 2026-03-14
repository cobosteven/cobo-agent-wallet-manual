---
name: cobo-agentic-wallet
description: |
  Use this skill whenever a task involves a Cobo Agentic Wallet — including wallet setup and onboarding, token transfers, contract calls, balance checks, transaction queries, and policy denial handling. Also use it for DeFi strategy execution: Aave V3 borrow/repay, DEX token swaps (Uniswap V3), DCA (dollar cost averaging), grid trading, and prediction market positions — on EVM chains (Ethereum, Base, Arbitrum, Optimism, Polygon) or Solana (mainnet via Jupiter, devnet). Use it whenever the user mentions `caw`, `cobo-agentic-wallet`, MPC wallet agent operations, or needs to call a smart contract or DeFi protocol through a policy-enforced wallet. Use it even if they don't say "Cobo" — if they're building or operating an AI agent that needs to spend crypto with guardrails, this skill applies.
---

# Cobo Agentic Wallet

Cobo Agentic Wallet is a policy-enforced crypto wallet for AI agents. Owners set spending limits and approve transactions; agents operate within those guardrails. The `caw` CLI is the primary interface.

> **Deterministic execution** — follow every step exactly, do NOT improvise or skip steps.

## Install

```bash
pip install --upgrade cobo-agentic-wallet
caw --help
```

> **Before using this skill**, ensure `cobo-agentic-wallet` is updated to the latest version. Commands and parameters may depend on the newest SDK.

## Environments

| Environment | `--env` value | API URL                                          | Web Console |
|-------------|---------------|--------------------------------------------------|-------------|
| Sandbox | `sandbox` | `https://api-agent-wallet-core.sandbox.cobo.com` | https://agenticwallet.sandbox.cobo.com/ |
| Dev | `dev` | `https://api-agentic-wallet-core.dev.cobo.com`   | https://agenticwallet.dev.cobo.com/ |

Set the API URL before any command:

```bash
export AGENT_WALLET_API_URL=https://api-agent-wallet-core.sandbox.cobo.com
```

## Onboarding Modes

Choose the mode that matches the user's instruction:

### Supervised Mode

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

### Autonomous Mode

No token needed. Agent self-provisions and creates its own wallet with no human owner.

```bash
caw onboard --create-wallet --env sandbox
```

This single command completes the full setup. **Do NOT create addresses or run self-test after this** — the wallet is ready to use immediately. Only proceed with further steps if the user explicitly requests them.

Notes:
- **`--sponsor false` is required** for transfers — autonomous wallets do NOT support gas sponsorship.

**Claiming — transfer ownership to a human:**

An autonomous wallet has no human owner. To let a human claim it:

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

See `caw profile --help` and [commands.md](./commands.md) for all profile subcommands (`list`, `current`, `use`, `env`, `archive`, `restore`).

> **ONLY use archive when a previous onboarding has failed and you need to retry.** Do NOT archive before a fresh onboarding — the `onboard` command creates a new profile automatically.

---

## Key Notes

- **`--sponsor` option**: `--sponsor true` (default) uses Cobo Gasless. Autonomous mode wallets must use `--sponsor false`.
- **TSS Node auto-start**: `caw tx transfer` and `caw tx call` automatically check TSS Node status and start it if offline.
- **`caw node stop`**: Checks for pending transactions before stopping. Use `--force` to skip the check.
- **wallet_uuid is optional** in most commands — if omitted, the CLI uses the active profile's wallet.

## Execution Guidance for AI Agents

1. **Always use `--format json`** for programmatic output. Use `--format table` only when displaying to the user.
2. **Long-running commands** (`caw onboard --create-wallet`): run in background, poll output every 10–15s, report each `[n/total]` progress step.
3. **Non-zero exit codes** indicate failure — always check stdout/stderr for details before retrying.
4. **Do NOT run `caw profile archive` before a fresh onboarding.** Only archive if a previous onboarding failed and you need to clean up before retrying.
5. **StandardResponse format** — API responses are wrapped as `{ success: true, result: <data> }`. When parsing JSON, extract from `result` first.

## Reference

Read the file that matches the user's task. Do not load files that aren't relevant.

- [CLI Command Reference](./commands.md) — Read when you need the full `caw` command syntax
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
