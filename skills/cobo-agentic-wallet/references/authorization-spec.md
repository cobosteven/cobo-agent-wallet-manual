# Pact Knowledge — Authorization Spec Construction

This document covers the core concepts and construction patterns for the authorization spec — the structured agreement that defines what an operator agent is authorized to do.

## Table of Contents

| Section | What it covers |
|---|---|
| [Authorization Spec Schema](#authorization-spec-schema) | Top-level fields, types, required vs optional |
| [Execution Plan Structure](#execution-plan-structure) | `execution_plan` markdown format and sections |
| [Permissions](#permissions) | Permission strings and least-privilege selection |
| [Policies](#policies) | Policy object schema (allow / deny) |
| [Policy Construction Patterns](#policy-construction-patterns) | Common patterns: transfer cap, contract scope, rolling limits |
| [Completion Conditions](#completion-conditions) | Auto-completion triggers |
| [Intent-to-Spec Construction Guide](#intent-to-spec-construction-guide) | Mapping user intent → spec fields |
| [Validation Rules](#validation-rules) | Rules enforced at submission time |
| [Lifecycle States](#lifecycle-states) | Pact status transitions |
| [Security Considerations](#security-considerations) | Least privilege, scope binding |

## Authorization Spec Schema

The authorization spec is the core data structure submitted with `caw pact submit`. It has five top-level fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `permissions` | string[] | Yes | Operations the operator is allowed to perform |
| `policies` | Policy[] | No | Rules that constrain permitted operations |
| `duration_seconds` | integer | No | How long the authorization remains active (null = no time limit) |
| `completion_conditions` | CompletionCondition[] | No | Auto-completion triggers |
| `resource_scope` | object | No | Resource binding (e.g., `{ "wallet_id": "..." }`) |
| `execution_plan` | string | No | Free-form execution plan derived from intent, in markdown format. Presented to the owner during approval review so they understand exactly what the operator will do. See [Execution Plan Structure](#execution-plan-structure) for suggested sections. |

## Execution Plan Structure

The `execution_plan` field is a free-form markdown string that describes the execution plan. It is presented to the wallet owner in the CAW App approval screen, helping them understand *what exactly* will happen if they approve the request.

**Suggested sections:**

| Section | Purpose |
|---|---|
| `# Summary` | One-line overview of the task |
| `# Contract Operations` | Which contracts/protocols will be called, on which chains, with what function signatures |
| `# Risk Controls` | Spending limits, slippage bounds, per-tx caps, stop-loss conditions |
| `# Schedule` | Cadence and timing (e.g. "every Monday", "once per hour for 7 days") |
| `# Exit Conditions` | When the execution stops (tx count, total spend, time elapsed) |

**Example:**

```markdown
# Summary
Weekly DCA: swap ~$500 USDC to ETH via Uniswap V3 on Base every Monday for 3 months.

# Contract Operations
- Protocol: Uniswap V3 SwapRouter
- Chain: Base
- Contract: 0x2626664c2603336E57B271c5C0b26F421741e481
- Function: exactInputSingle(ExactInputSingleParams)
- Token path: USDC -> WETH

# Risk Controls
- Max per swap: $550 USD
- Max daily: $600 USD
- Slippage tolerance: 0.5%
- Pre-swap balance check: skip if USDC balance < $500

# Schedule
- Frequency: every Monday at ~10:00 UTC
- Duration: 90 days from activation

# Exit Conditions
- After 12 successful swaps, OR
- After $6,000 total USD spent, OR
- After 90 days elapsed
```

The structure is not enforced — the agent should include whichever sections are relevant to the task. Simple tasks may only need `# Summary` and `# Contract Operations`. The key goal is to give the owner enough context to make an informed approve/reject decision.

## Permissions

Permissions define categories of allowed operations.

| Permission | Description |
|---|---|
| `read:wallet` | Read wallet info, addresses, balances, tx history |
| `write:wallet` | Create addresses, update wallet metadata |
| `write:transfer` | Initiate token transfers |
| `write:contract_call` | Execute smart contract calls |
| `write:manage` | Manage delegations (advanced) |

**Shorthand values:**

| Shorthand | Expands to |
|---|---|
| `viewer` | `read:wallet` |
| `operator` | `read:wallet` + `write:wallet` + `write:transfer` + `write:contract_call` |

Default: `operator`. Always apply **least privilege** — if the task only reads balances, use `viewer`.

### Bound API Key Enforcement

When an API key is bound to a delegation (for example, default pact bootstrap or pact activation), authorization must evaluate permissions from that bound delegation before any owner/controller fallback path.

Implementation requirement:

- If `api_keys.delegation_id` is updated after a key has already been authenticated, evict the in-memory API key verification cache entry for that key immediately.
- Rationale: stale cached key objects may still carry `delegation_id = null`, which can bypass bound-delegation evaluation and incorrectly hit owner/controller allow logic.
- Operational expectation: the very next request with that key must observe the latest `delegation_id` and be evaluated against delegation permissions.

## Policies

Policies constrain operations within granted permissions. Each policy targets a specific operation type (`transfer` or `contract_call`) and uses an `allow` or `deny` effect.

### Policy Structure

```json
{
  "name": "<human-readable-name>",
  "type": "transfer | contract_call",
  "rules": {
    "effect": "allow | deny",
    "when": { ... },
    "deny_if": { ... },
    "review_if": { ... },
    "always_review": false
  }
}
```

### Rules by Effect

| Field | `allow` effect | `deny` effect |
|---|---|---|
| `when` | Required (unless `always_review=true`) | Required |
| `deny_if` | Not allowed | Required (at least one limit) |
| `review_if` | Optional | Not allowed |
| `always_review` | Optional | Not allowed |

### Match Conditions (`when`)

**For `transfer` policies:**

| Field | Type | Description |
|---|---|---|
| `chain_in` | string[] | Restrict to specific chains (e.g. `["BASE", "ETH"]`) |
| `token_in` | ChainTokenRef[] | Restrict to specific tokens, e.g. `[{"chain_id":"BASE","token_id":"USDC"}]` |
| `destination_address_in` | ChainAddressRef[] | Restrict to specific destination addresses |

**For `contract_call` policies (EVM):**

| Field | Type | Description |
|---|---|---|
| `chain_in` | string[] | Restrict to specific chains |
| `target_in` | ContractTargetRef[] | Restrict to specific contract addresses and/or function selectors |
| `params_match` | ParameterConstraint[] | Constrain decoded calldata parameters |
| `function_abis` | object[] | Required when `params_match` is used; ABI definitions for calldata decoding |

**For `contract_call` policies (Solana):**

| Field | Type | Description |
|---|---|---|
| `chain_in` | string[] | Restrict to specific chains |
| `program_in` | ProgramRef[] | Restrict to specific program IDs |

### Deny Conditions (`deny_if`)

| Field | Type | Description |
|---|---|---|
| `amount_gt` | string (decimal) | Deny if single operation amount exceeds this (token units) |
| `amount_usd_gt` | string (decimal) | Deny if single operation USD value exceeds this |
| `usage_limits` | UsageLimits | Rolling-window budget limits |

**UsageLimits** — rolling time windows (each optional):

| Window | Description |
|---|---|
| `rolling_1h` | Hourly rolling window |
| `rolling_24h` | Daily rolling window |
| `rolling_7d` | Weekly rolling window |
| `rolling_30d` | Monthly rolling window |

Each window contains:

| Field | Type | Description |
|---|---|---|
| `amount_gt` | string (decimal) | Deny if cumulative amount in window exceeds this |
| `amount_usd_gt` | string (decimal) | Deny if cumulative USD value exceeds this |
| `tx_count_gt` | integer | Deny if transaction count in window exceeds this |

### Review Conditions (`review_if`)

Same structure as match conditions plus amount thresholds — matching operations require owner approval before execution.

| Field | Type | Description |
|---|---|---|
| `amount_gt` | string (decimal) | Require approval if amount exceeds this |
| `amount_usd_gt` | string (decimal) | Require approval if USD value exceeds this |

## Policy Construction Patterns

### Pattern: Allow + Deny Pair

A common pattern is pairing an `allow` policy with a `deny` policy for the same scope. For example, "allow Uniswap V3 swaps on Base, require review above $500, hard-deny above $550 or $600/day":

```json
[
  {
    "name": "dca-uniswap-allow",
    "type": "contract_call",
    "rules": {
      "effect": "allow",
      "when": {
        "chain_in": ["BASE"],
        "target_in": [{
          "chain_id": "BASE",
          "contract_addr": "0x2626664c2603336E57B271c5C0b26F421741e481"
        }]
      },
      "review_if": {
        "amount_usd_gt": "500"
      }
    }
  },
  {
    "name": "dca-uniswap-deny-limits",
    "type": "contract_call",
    "rules": {
      "effect": "deny",
      "when": {
        "chain_in": ["BASE"],
        "target_in": [{
          "chain_id": "BASE",
          "contract_addr": "0x2626664c2603336E57B271c5C0b26F421741e481"
        }]
      },
      "deny_if": {
        "amount_usd_gt": "550",
        "usage_limits": {
          "rolling_24h": { "amount_usd_gt": "600", "tx_count_gt": 5 }
        }
      }
    }
  }
]
```

### Pattern: Transfer-Only Policy

"Allow USDC transfers on Base to a specific address, deny above $1000/tx or $5000/day":

```json
[
  {
    "name": "usdc-transfer-allow",
    "type": "transfer",
    "rules": {
      "effect": "allow",
      "when": {
        "chain_in": ["BASE"],
        "token_in": [{ "chain_id": "BASE", "token_id": "USDC" }],
        "destination_address_in": [{
          "chain_id": "BASE",
          "address": "0xRecipientAddress..."
        }]
      }
    }
  },
  {
    "name": "usdc-transfer-deny-limits",
    "type": "transfer",
    "rules": {
      "effect": "deny",
      "when": {
        "chain_in": ["BASE"],
        "token_in": [{ "chain_id": "BASE", "token_id": "USDC" }]
      },
      "deny_if": {
        "amount_usd_gt": "1000",
        "usage_limits": {
          "rolling_24h": { "amount_usd_gt": "5000" }
        }
      }
    }
  }
]
```

### Pattern: Always-Review (No Auto-Approval)

When the owner wants to manually approve every operation:

```json
{
  "name": "always-review-all",
  "type": "transfer",
  "rules": {
    "effect": "allow",
    "always_review": true
  }
}
```

## Completion Conditions

Conditions that auto-terminate the authorization and revoke authorization when met. Multiple conditions use **any-of** semantics (first match triggers completion).

| Type | Threshold | Description |
|---|---|---|
| `time_elapsed` | seconds (string) | Complete after this duration from activation |
| `tx_count` | count (string) | Complete after this many transactions |
| `amount_spent_usd` | USD amount (string) | Complete after this total USD spend |
| `manual` | — | No auto-completion; runs until owner revokes |

Example:

```json
{
  "completion_conditions": [
    { "type": "tx_count", "threshold": "12" },
    { "type": "amount_spent_usd", "threshold": "6000" }
  ]
}
```

## Intent-to-Spec Construction Guide

When the user's intent is fully understood, the agent constructs an authorization spec by mapping intent components:

| Intent component | Spec field | Example |
|---|---|---|
| Goal / task description | `intent` (on submit request) | "Execute weekly ETH DCA on Base" |
| Required operation types | `permissions` | `["write:contract_call", "read:wallet"]` |
| Target chain / contract / token | `policies[].rules.when` | `chain_in`, `target_in`, `token_in` |
| Per-transaction budget | `policies[].rules.deny_if.amount_usd_gt` | `"550"` |
| Daily/rolling budget | `policies[].rules.deny_if.usage_limits` | `rolling_24h.amount_usd_gt` |
| Review threshold | `policies[].rules.review_if.amount_usd_gt` | `"500"` |
| Time window | `duration_seconds` | `7776000` (90 days) |
| Transaction count cap | `completion_conditions` | `{ "type": "tx_count", "threshold": "12" }` |
| Total spend cap | `completion_conditions` | `{ "type": "amount_spent_usd", "threshold": "6000" }` |
| Wallet binding | `resource_scope` | `{ "wallet_id": "<uuid>" }` |
| Step-by-step execution plan | `execution_plan` | Markdown with `# Summary`, `# Contract Operations`, `# Risk Controls`, `# Schedule`, `# Exit Conditions` — see [Execution Plan Structure](#execution-plan-structure) |

### Construction Checklist

Before submitting an authorization request, verify:

- [ ] `permissions` use least privilege — only what's needed for the task
- [ ] At least one `allow` policy with `when` conditions scoping the exact operations
- [ ] A matching `deny` policy with `deny_if` limits if budget constraints exist
- [ ] `duration_seconds` is set if the user specified a time window
- [ ] `completion_conditions` are set if the user specified tx count or budget caps
- [ ] `resource_scope` includes `wallet_id` to bind to the target wallet
- [ ] `execution_plan` describes the concrete execution steps when the task is multi-step or non-obvious
- [ ] `review_if` is used for soft thresholds that need owner attention but shouldn't block

## Validation Rules

The authorization service validates the spec at submission time (before creating the approval). Invalid specs return `422`:

- **`allow` policies**: must have non-empty `when` (unless `always_review=true`); cannot include `deny_if`
- **`deny` policies**: must have `deny_if` with at least one limit; cannot include `review_if` or `always_review`
- **`contract_call` with `params_match`**: must include `function_abis`
- **`completion_conditions`**: must use valid types (`time_elapsed`, `tx_count`, `amount_spent_usd`, `manual`)
- **`permissions`**: must be valid values from the permission set

## Lifecycle States

```
POST /pacts/submit ──► PENDING_APPROVAL
                            |
                       ┌────┴────┐
                approved|        |rejected
                       v        v
                    ACTIVE   REJECTED
                   ┌──┬──┐
                   |  |  |
conditions met <───┘  |  └──> owner revokes ──> REVOKED
    v                 v
COMPLETED          EXPIRED
```

| State | Description |
|---|---|
| `PENDING_APPROVAL` | Submitted, awaiting owner approval in CAW App |
| `REJECTED` | Owner rejected the request |
| `ACTIVE` | Delegation + policies created, operator can act |
| `COMPLETED` | Completion condition met, authorization revoked |
| `EXPIRED` | Duration elapsed, authorization revoked |
| `REVOKED` | Owner manually revoked, authorization revoked |

## Security Considerations

- The owner has absolute authority — no authorization activates without explicit approval
- The authorization-scoped API key is only visible to the submitting operator
- The API key is bound to a specific delegation — cannot be used outside authorization scope
- The API key is invalidated when the authorization reaches any terminal state
- Operators should construct the most restrictive spec that fulfills the task

