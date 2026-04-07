# Pact Management

Pact management is implemented via the `caw pact` CLI commands.

## When to use pact

> ⚠️ **Important**:
> - All on-chain transactions MUST go through a pact. Default pacts cannot be used for any transaction — always create a new pact or use an existing non-default pact.
> - On-chain transactions include: token transfers (`caw tx transfer`), contract calls (`caw tx call`), and message signing (`caw tx sign-message`). Read-only queries (`caw wallet balance`, `caw tx list`, etc.) do not require a pact.

## Pact Submission Flow

1. **Dedup check**: `caw pact list --status pending_approval --wallet-id <id>`. If a pending request exists with the same intent, do NOT submit.

2. **Construct** pact parameters from the user's intent (see [Intent -> Submit Parameter Mapping](#intent---submit-parameter-mapping))

3. **Pre-submit preview** — always present a human-readable summary of the **4 core items** before submitting:

   | # | Item | What to show |
   |---|------|-------------|
   | 1 | 🎯 **Intent** | One-sentence goal: what asset, what action, which chain |
   | 2 | 📝 **Execution Plan** | 2–4 bullet summary of what will happen |
   | 3 | 📜 **Policies** | Per-tx cap, daily cap, scope/chain/token/contract restrictions |
   | 4 | 🏁 **Completion Conditions** | Time limit, total spend cap, tx count |

   **After the preview**, ask for explicit user confirmation. Never submit a pact without user sign-off.

4. **Submit**: `caw pact submit ...`

5. **Communicate**: Tell the user the pact has been submitted and ask for their confirmation in this conversation. If the wallet is paired, also let them know they will need to approve it in the **Human App** as well.

6. **Track**: `caw track --watch` starts automatically after submission.

7. **On `active`**: Immediately reply that pact was approved, then trigger execution as **background task** via `exec background:true`.

8. **On `rejected`**: Tell the user "The owner declined this action."

## CLI Command Reference

### `caw pact submit`

Submit a new pact for owner approval.

**Required flags:**

| Flag | Description |
|---|---|
| `--wallet-id <uuid>` | Target wallet UUID |
| `--intent <text>` | Natural language description of the pact's purpose |

**Optional flags:**

| Flag | Default | Description |
|---|---|---|
| `--duration <seconds>` | `0` (no expiry) | Pact duration in seconds from activation |
| `--max-tx <usd>` | — | Maximum USD value per transaction (inline policy shortcut) |
| `--name <text>` | derived | Human-readable name for owner review |
| `--resource-scope <json>` | — | Resource scope, e.g. `'{"wallet_id":"<uuid>"}'` |
| `--execution-plan <text>` | — | Markdown execution plan shown to owner |
| `--original-intent <text>` | — | Raw user input that triggered this request |
| `--spec-file <path>` | — | Full pact spec JSON file (mutually exclusive with inline flags) |
| `--spec-json <json>` | — | Full pact spec JSON inline |
| `--context <json>` | **required** | `{"channel":"<>", "target":"<>", "session_id":"<uuid>"}` |

### `caw pact show <pact-id>`

Show full details of a specific pact. Triggers lazy activation if approved.

### `caw pact list`

List pacts with optional filters: `--status`, `--wallet-id`, `--limit`, `--offset`.

### `caw pact events <pact-id>`

Get lifecycle event history for a pact.

### `caw pact revoke <pact-id>`

Revoke an active pact. **Wallet owner only.**

### `caw pact withdraw <pact-id>`

Withdraw a pending pact. **Operator only.**

## Least Privilege Principle

**Duration and completion**: Always set finite bounds. Avoid `0` duration unless user explicitly requests indefinite access.

**Resource scope**: Always bind to the target wallet via `--resource-scope '{"wallet_id":"<uuid>"}'`.

## Intent -> Submit Parameter Mapping

| Intent component | Flag | Mapping guidance |
|---|---|---|
| Target wallet | `--wallet-id` | Exact wallet UUID |
| Goal description | `--intent` | Normalized goal including asset/protocol/chain |
| Time window | `--duration` | Parse explicit time: `30d` -> `2592000`, `3 months` -> `7776000` |
| Per-transaction budget | `--max-tx` | Per-transaction USD cap (inline policy shortcut) |
| Custom policies | `--spec-file` / `--spec-json` | For chain/token/contract scoping, allow/deny pairs, rolling limits |
| Display name | `--name` | Concise title for owner review |
| Execution plan | `--execution-plan` | Markdown with `# Summary`, `# Contract Operations`, `# Risk Controls` |
| Raw user input | `--original-intent` | User's original message(s) verbatim |

## Post-Submission Flow

### Background Execution Rule

**Any execution triggered by a notification MUST use `exec background:true`. Never synchronously wait for tx results.**

Pattern:
1. Reply immediately: "Authorization approved — starting execution now."
2. Trigger execution via `exec background:true`
3. Report results when complete

### Executing Under a Pact

When the request becomes `active`, pass the `<pact-id>` as the first positional argument to tx commands. The CLI resolves the wallet UUID and API key from the pact automatically — do not pass `--wallet-id` separately.

```bash
# The pact_id is returned in the pact submit / show response
caw tx transfer <pact_id> --token-id USDC --to 0x... --amount 10 ...
caw tx call <pact_id> --chain-id BASE_ETH --contract 0x... --calldata 0x...
caw tx sign-message <pact_id> --chain-id ETH --destination-type eip712 --eip712-typed-data '{...}'
```

## Policy Reference

Policies constrain operations within a pact. Each policy targets a specific operation type (`transfer`, `contract_call`, or `message_sign`) and always uses `allow` effect.

> 🔒 **Default-deny semantics**: Any operation that does **not** match the `when` conditions of at least one `allow` policy is **automatically denied**. There is no implicit pass-through — if a transaction type, chain, token, or contract is not explicitly covered by a policy, the request is rejected. Always define policies that explicitly cover every operation the agent needs to perform.

### Policy Structure

```json
{
  "name": "<human-readable-name>",
  "type": "transfer | contract_call | message_sign",
  "rules": {
    "effect": "allow",
    "when": { ... },
    "deny_if": { ... },
    "review_if": { ... }
  }
}
```

| Field | Required | Description |
|---|---|---|
| `effect` | Yes | Always set to `"allow"`.|
| `when` | Yes (unless `always_review=true`) | Allowlist conditions — which chains/tokens/contracts/domains to permit |
| `deny_if` | Optional | Hard-block conditions — usage limits that trigger an automatic deny |
| `review_if` | Optional | Soft-block conditions — thresholds that require owner approval before proceeding |

### Allowlist Conditions (`when`)

**For `transfer` policies:**

| Field | Type | Description |
|---|---|---|
| `chain_in` | string[] | Restrict to specific chains (e.g. `["BASE_ETH", "ETH"]`) |
| `token_in` | ChainTokenRef[] | Restrict to specific tokens, e.g. `[{"chain_id":"BASE_ETH","token_id":"BASE_USDC"}]` |
| `destination_address_in` | ChainAddressRef[] | Restrict to specific destination addresses |

**For `contract_call` policies (EVM):**

| Field | Type | Description |
|---|---|---|
| `chain_in` | string[] | Restrict to specific chains |
| `target_in` | ContractTargetRef[] | Restrict to specific contract addresses and/or function selectors |

**For `contract_call` policies (Solana):**

| Field | Type | Description |
|---|---|---|
| `chain_in` | string[] | Restrict to specific chains |
| `program_in` | ProgramRef[] | Restrict to specific program IDs |

### Usage Limits (`deny_if`)

| Field | Type | Description |
|---|---|---|
| `amount_usd_gt` | string (decimal) | Deny if single operation USD value exceeds this |
| `usage_limits` | UsageLimits | Rolling-window budget limits |

**UsageLimits** — supports `rolling_24h` window:

| Field | Type | Description |
|---|---|---|
| `amount_usd_gt` | string | Deny if cumulative USD value in the 24 h window exceeds this |
| `tx_count_gt` | integer | Deny if transaction count in the 24 h window exceeds this |

### Review USD Threshold (`review_if`)

Matching operations require owner approval before execution.

| Field | Type | Description |
|---|---|---|
| `amount_gt` | string (decimal) | Require approval if amount exceeds this |
| `amount_usd_gt` | string (decimal) | Require approval if USD value exceeds this |

### Message Sign Policies

`message_sign` policies control EIP-712 typed-data signing. Use `domain_match` to restrict which dApps/contracts can request signatures.

**`when.domain_match`** — list of `ParameterConstraint`:

| Field | Type | Description |
|---|---|---|
| `param_name` | string | Field name in the EIP-712 `domain` object (e.g. `"name"`, `"verifyingContract"`) |
| `op` | string | Operator: `eq`, `neq`, `in`, `not_in` |
| `value` | any | Value to compare against |

**`deny_if`** (optional) — supports `usage_limits.rolling_24h.request_count_gt` only.

**`review_if`** (optional) — supports same fields as `when`.

**Example — restrict Permit2 signatures to a specific contract:**

```json
{
  "name": "permit2-sign",
  "type": "message_sign",
  "rules": {
    "effect": "allow",
    "when": {
      "domain_match": [
        { "param_name": "name", "op": "eq", "value": "Permit2" },
        { "param_name": "verifyingContract", "op": "eq", "value": "0x000000000022D473030F116dDEE9F6B43aC78BA3" }
      ]
    },
    "deny_if": {
      "usage_limits": { "rolling_24h": { "request_count_gt": 50 } }
    }
  }
}
```

### USD Pricing Note

> ⚠️ **Important**: USD-based policy conditions (`amount_usd_gt`, `usage_limits.rolling_24h.amount_usd_gt`) only apply to tokens with available price data. **Tokens without price data will NOT be affected by USD-based policies** — they will bypass these thresholds entirely. Combine with explicit `token_in` allowlists to ensure only priced tokens are permitted.

## Policy Construction Patterns

### Pattern: Allow with Inline Limits

"Allow Uniswap V3 swaps on Base, require review above $500, hard-deny above $550 or $600/day":

```json
[
  {
    "name": "dca-uniswap-allow",
    "type": "contract_call",
    "rules": {
      "effect": "allow",
      "when": {
        "chain_in": ["BASE_ETH"],
        "target_in": [{ "chain_id": "BASE_ETH", "contract_addr": "0x2626664c2603336E57B271c5C0b26F421741e481" }]
      },
      "deny_if": {
        "amount_usd_gt": "550",
        "usage_limits": { "rolling_24h": { "amount_usd_gt": "600", "tx_count_gt": 5 } }
      }
    }
  }
]
```

### Pattern: Transfer with Allowlist + Limits

"Allow USDC transfers on Base to a specific address, deny above $1000/tx or $5000/day":

```json
[
  {
    "name": "usdc-transfer-allow",
    "type": "transfer",
    "rules": {
      "effect": "allow",
      "when": {
        "chain_in": ["BASE_ETH"],
        "token_in": [{ "chain_id": "BASE_ETH", "token_id": "BASE_USDC" }],
        "destination_address_in": [{ "chain_id": "BASE_ETH", "address": "0xRecipientAddress..." }]
      },
      "deny_if": {
        "amount_usd_gt": "1000",
        "usage_limits": { "rolling_24h": { "amount_usd_gt": "5000" } }
      }
    }
  }
]
```

### Pattern: Blacklist via Empty `deny_if`

An empty `deny_if: {}` unconditionally denies any operation that matches the `when` clause. This turns an `allow` policy into a targeted blacklist — useful for blocking an entire chain, token, or contract.

"Block all transfers on BSC regardless of amount":

```json
[
  {
    "name": "block-bsc-transfers",
    "type": "transfer",
    "rules": {
      "effect": "allow",
      "when": { "chain_in": ["BSC_BNB"] },
      "deny_if": {}
    }
  }
]
```

## Handling Outcomes

| Status | Agent action |
|---|---|
| `pending_approval` | Ask the user to confirm in this conversation. If the wallet is paired, also notify them that approval in the **Human App** is required. |
| `active` | Proceed with execution within pact scope |
| `rejected` | Surface rejection to user; ask whether to adjust constraints |
| `revoked` / `expired` / `completed` | Stop execution; inform user; submit new pact if needed |
