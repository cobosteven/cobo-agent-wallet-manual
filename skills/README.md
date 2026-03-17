# cobo-agentic-wallet skill

A Claude Code skill that enables AI agents to operate Cobo Agentic Wallets, aka caw or CAW — policy-enforced crypto wallets with spending limits, approval workflows, and DeFi strategy execution.

## Install

### 1. Install the skill

在 Claude Code 或其他支持 skill 的 AI 工具中直接说：

```
Install the cobo-agentic-wallet skill from https://github.com/cobosteven/cobo-agent-wallet-manual/tree/master/skills/cobo-agentic-wallet
```

### 2. Try it

安装后尝试以下 prompt：

```
"Set up a Cobo Agentic Wallet for my AI agent on sandbox"
"Send 10 USDC to 0xABC...123 on Base from my agent wallet"
"DCA 0.01 SOL into USDC every hour for 24 hours on Solana devnet"
"My transfer got TRANSFER_LIMIT_EXCEEDED, how do I fix it?"
```

## Use with AI agents

### System prompt for AI agents

If you are building an AI agent that manages crypto wallets, add the following to your agent's system prompt to enable wallet operations:

```
You have access to a Cobo Agentic Wallet for managing crypto assets with policy enforcement.

Environment:
- API URL: https://api-agent-wallet-core.sandbox.cobo.com
- API Key: (stored in AGENT_WALLET_API_KEY)
- CLI tool: caw

Core capabilities:
- Transfer tokens: caw tx transfer <wallet_uuid> --to <addr> --token <id> --amount <n> --chain <id>
- Contract call: caw tx call <wallet_uuid> --contract <addr> --calldata <hex> --chain <id>
- Check balance: caw wallet balance <wallet_uuid>
- List transactions: caw tx list <wallet_uuid>

Always use --format json for programmatic output. Check exit code and stderr on failure.
If a transfer is denied by policy, parse the error's "suggestion" field for the corrected parameters.
See error-handling.md for common error codes and recovery steps.

For DeFi operations (Uniswap swaps, Aave lending, Jupiter swaps on Solana), use caw tx call
with the appropriate contract address and calldata.
```

### Claude Code — CLAUDE.md configuration

Add to your project's `.claude/CLAUDE.md`:

```markdown
## Wallet Operations

This project uses Cobo Agentic Wallet for crypto operations.
The cobo-agentic-wallet skill is installed at ./skills/cobo-agentic-wallet/.

When asked to perform wallet operations:
1. Use `caw` CLI with `--format json` for all commands
2. Always check transaction status after submission
3. Handle policy denials by parsing the suggestion field — see [Error Handling](./skills/cobo-agentic-wallet/recipes/error-handling.md)
4. For DeFi operations, refer to the skill's recipe files
```

### Post-setup persistence

After completing wallet setup, the agent should save the following to its memory or config file:

| Field | Source | Purpose |
|-------|--------|---------|
| `wallet_uuid` | `caw profile current` | Identify the wallet |
| `api_key` | onboard output | Authenticate API calls |
| `addresses` | `caw address list` | Per-chain addresses |
| `env` | setup parameter | sandbox/dev/prod |
| `policy_limits` | `caw policy list` (if available) | Know spending constraints |

Without this, the agent loses wallet context on restart and must re-onboard or re-discover.

## DeFi recipes

Each recipe supports both testnet (simulation) and mainnet (real execution):

| Strategy | EVM | Solana |
|----------|-----|--------|
| DEX Swap | [Uniswap V3](./cobo-agentic-wallet/recipes/evm-defi-dex-swap.md) | [Jupiter V6](./cobo-agentic-wallet/recipes/solana-defi-dex-swap.md) |
| DCA | [EVM DCA](./cobo-agentic-wallet/recipes/evm-defi-dca.md) | [Solana DCA](./cobo-agentic-wallet/recipes/solana-defi-dca.md) |
| Grid Trading | [EVM Grid](./cobo-agentic-wallet/recipes/evm-defi-grid-trading.md) | [Solana Grid](./cobo-agentic-wallet/recipes/solana-defi-grid-trading.md) |
| Lending | [Aave V3](./cobo-agentic-wallet/recipes/evm-defi-aave.md) | — |
| Prediction Market | — | [Drift / Polymarket](./cobo-agentic-wallet/recipes/solana-defi-prediction-market.md) |

Also see: [Policy Management](./cobo-agentic-wallet/recipes/policy-management.md) | [Error Handling](./cobo-agentic-wallet/recipes/error-handling.md)

## Evals

Validate the skill works correctly:

```bash
cd cobo-agentic-wallet/evals/

./run_evals.sh trigger   # 20 tests: does the skill trigger correctly?
./run_evals.sh quality   # 6 tests: does the output contain correct commands?
./run_evals.sh all       # Run both (~5 min, requires claude CLI)
```

## Updating the skill

Skill 分为三个版本：canonical source 和两个环境特定版本。

1. **编辑 canonical source** — 修改 `cobo-agentic-wallet/` 下的文件（SKILL.md、recipes 等）
2. **运行同步脚本** — 将改动同步到 sandbox 和 dev 版本：

```bash
cd skills/
python3 sync_env_skills.py
```

脚本会从 canonical source 自动生成 `cobo-agentic-wallet-sandbox/` 和 `cobo-agentic-wallet-dev/` 的全部内容（SKILL.md + recipes），环境相关的字段（name、URL、`--env` 值）自动替换。

> **不要直接编辑** `cobo-agentic-wallet-sandbox/` 或 `cobo-agentic-wallet-dev/` 下的文件，下次运行同步脚本会被覆盖。

## File structure

```
skills/
├── README.md                            # This file
├── sync_env_skills.py                   # Sync canonical → env-specific skills
├── cobo-agentic-wallet/                 # Canonical source (edit here)
│   ├── SKILL.md                         # Main instructions (loaded on trigger)
│   ├── commands.md                      # caw CLI command reference
│   ├── recipes.md                       # Recipe index
│   ├── recipes/                         # DeFi + operational recipes
│   ├── scripts/
│   │   └── convert_jupiter.sh           # Jupiter API → caw CLI format converter
│   └── evals/
│       ├── trigger-eval.json            # Trigger accuracy tests
│       ├── evals.json                   # Output quality tests
│       └── run_evals.sh                 # Eval runner script
├── cobo-agentic-wallet-sandbox/         # Auto-generated (sandbox env)
│   ├── SKILL.md
│   ├── recipes.md
│   └── recipes/
└── cobo-agentic-wallet-dev/             # Auto-generated (dev env)
    ├── SKILL.md
    ├── recipes.md
    └── recipes/
```
