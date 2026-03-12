---
name: cobo-agentic-wallet
description: |
  Use this skill for any crypto wallet operation: sending tokens, calling contracts, checking balances, querying transactions, or handling policy denials.
---

# cobo-agentic-wallet

Operate wallets through the `caw` CLI with policy enforcement. Owners configure limits and approve transactions; agents execute within those guardrails.

## Install

```bash
pip install cobo-agentic-wallet
caw --help
```

## Auth Setup

Credentials are auto-stored by `caw onboard provision`. Set API URL before running any command:

```bash
export AGENT_WALLET_API_URL=https://api-agent-wallet-core.sandbox.cobo.com
export AGENT_WALLET_API_KEY=<your_api_key>  # obtained from caw onboard provision output
```

## Quick Start

### Onboarding

**Web Console (Sandbox):** https://agenticwallet.sandbox.cobo.com/ — use it to get a setup token, set policies, and delegate wallets.

1. Owner opens the [Web Console](https://agenticwallet.sandbox.cobo.com/) or Human App and gets a setup token.
2. Agent runs:
   ```bash
   caw --format table onboard provision --token <TOKEN>
   ```
3. An API key is created and bound to the owner's account. Write it to your environment:
   ```bash
   export AGENT_WALLET_API_KEY=<api_key_from_output>
   ```
4. If instructed, create a wallet and address:
   ```bash
   caw wallet create --name <name> --main-node-id <id>
   caw address create <wallet_uuid> --chain <id>
   ```
5. Print a summary table with `agent_id`, `wallet_uuid`, `address` (if created), and config paths. If `wallet_uuid` is empty, remind the user to go to the Human App to delegate a wallet to this agent, then return to start using it.
6. Validate setup:
   ```bash
   caw --format json onboard self-test --wallet <wallet_uuid>
   ```
7. First transfer:
   ```bash
   caw --format json tx transfer <wallet_uuid> \
     --to 0x1234...abcd --token USDC --amount 1 --chain BASE
   ```

### Execution guidance for AI agents

- For any long-running command: run in background, poll output every 10-15 seconds, and report progress to the user as each step completes.
- Currently long-running commands: `caw wallet create` (60-180 seconds). Progress steps are printed to stdout in the format `[n/total] Step description... done`.
- Any non-zero exit code indicates failure -- check output for error details.

## Reference

- [CLI Command Reference](./commands.md) — Full list of all `caw` commands
- [Recipes](./recipes.md) — Common operations and usage scenarios
  - [Policy Management](./recipes/policy-management.md)
  - [Error Handling](./recipes/error-handling.md)