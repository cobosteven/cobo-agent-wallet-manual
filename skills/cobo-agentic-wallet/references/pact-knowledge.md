# Pact Knowledge

## Table of Contents

- [What is a Pact](#what-is-a-pact)
- [Pact Lifecycle](#pact-lifecycle)
  - [owner\_linked = false — Pact auto-activates (no pair yet)](#owner_linked--false--pact-auto-activates-no-pair-yet)
  - [owner\_linked = true — Pact requires human approval](#owner_linked--true--pact-requires-human-approval)
  - [State Reference](#state-reference)
- [Pact Spec Schema](#pact-spec-schema)
- [Parameter Construction Guide](#parameter-construction-guide)
  - [Duration Conversion](#duration-conversion)
  - [Policy Approach Decision](#policy-approach-decision)
- [Execution Plan Structure](#execution-plan-structure)
- [Permissions](#permissions)
  - [Bound API Key Enforcement](#bound-api-key-enforcement)
- [Policies](#policies)
  - [Policy Structure](#policy-structure)
  - [Rules by Effect](#rules-by-effect)
  - [Match Conditions (`when`)](#match-conditions-when)
  - [Deny Conditions (`deny_if`)](#deny-conditions-deny_if)
  - [Review Conditions (`review_if`)](#review-conditions-review_if)
- [Policy Construction Patterns](#policy-construction-patterns)
  - [Pattern: Allow + Deny Pair](#pattern-allow--deny-pair)
  - [Pattern: Transfer-Only Policy](#pattern-transfer-only-policy)
  - [Pattern: Always-Review (No Auto-Approval)](#pattern-always-review-no-auto-approval)
- [Completion Conditions](#completion-conditions)
- [Validation Rules](#validation-rules)
- [Security Considerations](#security-considerations)

## What is a Pact

A **pact** is a time-limited, policy-bound delegation that authorizes an operator agent to act on behalf of a wallet within defined constraints. It is the mechanism by which an owner grants a scoped, auditable set of permissions to an autonomous agent.

A pact defines:

| Aspect | Field | Description |
|---|---|---|
| What the agent can do | `permissions` | Categories of allowed operations (transfer, contract call, etc.) |
| Under what constraints | `policies` | Allow/deny rules that limit scope, amounts, and targets |
| For how long | `duration_seconds` | Time window from activation; null = no expiry |
| Under what conditions | `completion_conditions` | Auto-termination triggers (tx count, spend cap, time elapsed) |
| To which wallet | `resource_scope` | Wallet binding |
| What the agent plans to do | `execution_plan` | Shown to the owner at review time |

Once active, the operator uses a **pact-scoped API key** (returned at activation) for all operations under the pact. The key is automatically invalidated when the pact expires, is completed, or is revoked.

## Pact Lifecycle

The lifecycle differs depending on whether the wallet has been paired with an owner (`owner_linked`).

### owner_linked = false — Pact auto-activates (no pair yet)

`owner_linked = false` means the wallet has not yet been paired with an owner in the Human App. There is no human approver, so submitted pacts **activate immediately** without any review step.

**Because activation is immediate and irreversible, the agent MUST get explicit operator confirmation before submitting.**

Flow:

```
1. Check:  caw status → owner_linked = false
2. Show:   Pre-submit preview to operator (intent, permissions, policies, duration, execution_plan)
3. Confirm: Wait for operator to explicitly confirm ("yes" / "proceed" / etc.)
4. Submit: caw pact submit ... → pact enters ACTIVE immediately
5. Execute: use the returned pact-scoped API key
```

State path:

```
POST /pacts/submit ──► ACTIVE (immediate, no approval step)
                          |
              ┌───────────┼───────────┐
              v           v           v
         COMPLETED     EXPIRED     REVOKED
```

### owner_linked = true — Pact requires human approval

`owner_linked = true` means the wallet has been paired with an owner via the CAW App. The owner must review and approve the pact before it becomes active.

Flow:

```
1. Check:  caw status → owner_linked = true
2. Submit: caw pact submit ... → pact enters PENDING_APPROVAL
3. Notify: Tell the user the owner must approve in the CAW App
4. Wait:   Poll or wait for notification: approved → ACTIVE / rejected → REJECTED
5. Execute: use the returned pact-scoped API key (on ACTIVE notification)
```

State path:

```
POST /pacts/submit ──► PENDING_APPROVAL
                              |
                        ┌─────┴─────┐
                 approved|           |rejected
                        v            v
                     ACTIVE       REJECTED
                        |
            ┌───────────┼───────────┐
            v           v           v
       COMPLETED     EXPIRED     REVOKED
```

### State Reference

| State | Description |
|---|---|
| `PENDING_APPROVAL` | Submitted, awaiting owner approval in CAW App (`owner_linked=true` only) |
| `ACTIVE` | Delegation + policies in effect; pact-scoped API key is usable |
| `REJECTED` | Owner rejected; pact-scoped key is invalid |
| `COMPLETED` | Completion condition met; pact-scoped key invalidated |
| `EXPIRED` | Duration elapsed; pact-scoped key invalidated |
| `REVOKED` | Owner manually revoked; pact-scoped key invalidated |

## Pact Spec Schema

The pact spec is the core data structure submitted with `caw pact submit`. It has five top-level fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `permissions` | string[] | Yes | Operations the operator is allowed to perform |
| `policies` | Policy[] | No | Rules that constrain permitted operations |
| `duration_seconds` | integer | No | How long the pact remains active (null = no time limit) |
| `completion_conditions` | CompletionCondition[] | No | Auto-completion triggers |
| `resource_scope` | object | No | Resource binding (e.g., `{ "wallet_id": "..." }`) |
| `execution_plan` | string | No | Free-form execution plan derived from intent, in markdown format. Presented to the owner during approval review so they understand exactly what the operator will do. See [Execution Plan Structure](#execution-plan-structure) for suggested sections. |

## Parameter Construction Guide

When constructing pact parameters from user intent, map each intent component to the appropriate field or CLI flag:

| Intent component | Field / Flag | Guidance |
|---|---|---|
| Goal description | `--intent` | One sentence covering: asset, action, chain, cadence, and key risk constraints (e.g. "DCA $500/week into ETH on Base for 3 months") |
| Target wallet | `--wallet-id` | Exact wallet UUID — always required |
| Required operations | `--permissions` | Minimum permission set for the task. See [Permissions](#permissions). |
| Time window | `--duration` / `duration_seconds` | Parse explicit time (see conversion table below). Prefer finite duration. |
| Per-transaction cap | `--max-tx` or `deny_if.amount_usd_gt` | Use `--max-tx` for a simple USD cap; use full policy spec when chain/token/contract scoping or rolling limits are needed. See [Policy approach decision](#policy-approach-decision) below. |
| Resource binding | `--resource-scope` / `resource_scope` | Always bind to the target wallet: `{"wallet_id":"<uuid>"}`. Narrows blast radius if the pact-scoped key is misused. |
| Display name | `--name` | Concise human-readable title for owner review (e.g. "Base ETH Weekly DCA") |
| Execution description | `--execution-plan` / `execution_plan` | Markdown execution plan shown to owner during approval. See [Execution Plan Structure](#execution-plan-structure). Write in plain language — avoid raw addresses or hex calldata in the summary. |
| Raw user input | `--original-intent` | User's verbatim message(s). Single-turn: the triggering message. Multi-turn: concatenate all messages relevant to this operation as `"User: <msg1>\nUser: <msg2>"`. Omit unrelated messages. |
| Total budget / tx count cap | `completion_conditions` | Set when the full task scope is bounded. See [Completion Conditions](#completion-conditions). |

### Duration Conversion

| Human expression | Seconds |
|---|---|
| 7 days | `604800` |
| 30 days | `2592000` |
| 3 months | `7776000` |
| 6 months | `15552000` |
| 1 year | `31536000` |

Prefer finite duration. Use `0` (no expiry) only when the user explicitly requests indefinite access.

### Policy Approach Decision

| Scenario | Approach |
|---|---|
| Simple per-transaction USD cap, no other constraints | `--max-tx <usd>` inline flag |
| Chain / token / contract scoping, rolling limits, or `review_if` thresholds | `--spec-json` or `--spec-file` with full policies array |
| Complex multi-policy setup or completion conditions | `--spec-file` for readability |

See [Policy Construction Patterns](#policy-construction-patterns) for full schema and examples.

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
- Wallet pairing confirmation must not clear existing API key `delegation_id` / `pact_id` bindings; preserving bound keys keeps pre-pairing active pact keys valid after ownership transfer.
- For bound keys tied to the **default pact**, permission denials on `can_transfer`, `can_call_contract`, and `can_message_sign` should include an `INSUFFICIENT_PERMISSION` suggestion that tells the caller to create a pact and retry with that pact.

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
| `chain_in` | string[] | Restrict to specific chains using chain identifiers (e.g. `["BASE_ETH", "ETH"]`) — same identifiers as `--chain-id` in the CLI (e.g. `ETH`, `BASE_ETH`, `ARBITRUM_ETH`, `SOL`) |
| `token_in` | ChainTokenRef[] | Restrict to specific tokens, e.g. `[{"chain_id":"BASE_ETH","token_id":"BASE_USDC"}]` — use token IDs as returned by `caw meta tokens` |
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
        "chain_in": ["BASE_ETH"],
        "target_in": [{
          "chain_id": "BASE_ETH",
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
        "chain_in": ["BASE_ETH"],
        "target_in": [{
          "chain_id": "BASE_ETH",
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
        "chain_in": ["BASE_ETH"],
        "token_in": [{ "chain_id": "BASE_ETH", "token_id": "BASE_USDC" }],
        "destination_address_in": [{
          "chain_id": "BASE_ETH",
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
        "chain_in": ["BASE_ETH"],
        "token_in": [{ "chain_id": "BASE_ETH", "token_id": "BASE_USDC" }]
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

Conditions that auto-terminate the pact and revoke it when met. Multiple conditions use **any-of** semantics (first match triggers completion).

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

## Validation Rules

The pact service validates the spec at submission time (before creating the approval). Invalid specs return `422`:

- **`allow` policies**: must have non-empty `when` (unless `always_review=true`); cannot include `deny_if`
- **`deny` policies**: must have `deny_if` with at least one limit; cannot include `review_if` or `always_review`
- **`contract_call` with `params_match`**: must include `function_abis`
- **`completion_conditions`**: must use valid types (`time_elapsed`, `tx_count`, `amount_spent_usd`, `manual`)
- **`permissions`**: must be valid values from the permission set

## Security Considerations

- The pact-scoped API key is only visible to the submitting operator
- The API key is bound to a specific delegation — cannot be used outside pact scope
- The API key is invalidated when the pact reaches any terminal state
- Operators should construct the most restrictive spec that fulfills the task
