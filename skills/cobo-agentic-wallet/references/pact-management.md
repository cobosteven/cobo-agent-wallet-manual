# Pact Management

Pact management is implemented via the `caw pact` CLI commands.

## Table of Contents

- [When to use pact](#when-to-use-pact)
- [Pact Submission Flow](#pact-submission-flow)
- [Transfer Quota Exhaustion Fallback](#transfer-quota-exhaustion-fallback)
- [Lifecycle](#lifecycle)
- [CLI Command Reference](#cli-command-reference)
  - [`caw pact submit`](#caw-pact-submit)
  - [`caw pact get <pact-id>`](#caw-pact-get-pact-id)
  - [`caw pact list`](#caw-pact-list)
  - [`caw pact events <pact-id>`](#caw-pact-events-pact-id)
  - [`caw pact cancel <pact-id>`](#caw-pact-cancel-pact-id)
- [Least Privilege Principle](#least-privilege-principle)
- [Intent -> Submit Parameter Mapping](#intent---submit-parameter-mapping)
- [Submission Rules](#submission-rules)
- [Post-Submission Flow](#post-submission-flow)
  - [Background Execution Rule](#background-execution-rule)
  - [Tracking Approval](#tracking-approval)
  - [Using the Pact-Scoped API Key](#using-the-pact-scoped-api-key)
- [Handling Outcomes](#handling-outcomes)
- [Troubleshooting](#troubleshooting)

## When to use pact

> **Note**: Always check `owner_linked` from `caw --format json status` first. When `owner_linked = false`, submitted pacts **auto-activate** (no human approval required) — see [Pact Submission Flow](#pact-submission-flow) Step 1 for the required confirmation step before submission.

**Decision principle**: Evaluate the full end-to-end complexity of the user's intent. If completing the task involves on-chain writes (contract calls, message signing), spans time (recurring, scheduled, multi-phase), requires coordinated multi-step execution, or carries financial risk beyond a simple one-shot transfer — request a pact. When in doubt, request a pact. Never suggest the user manually configure controls in WebConsole as a substitute for this flow.

**Pact required:**
- Contract calls (`caw tx call`) or message signing (`caw tx sign-message`)
- Any task that spans time or repeats (recurring transfers, DCA, periodic rebalancing, scheduled automation)
- Multi-step workflows with risk bounds
- Temporary delegated access with explicit expiry
- Transfer denied due to cumulative quota exhaustion (daily/monthly limit reached)

**Direct execution (no pact):**
- Single token transfer within default quota — try `caw tx transfer` first
- Read-only queries (`caw wallet balance`, `caw tx list`, etc.)
- Per-transaction limit denial — suggest reduced amount or ask user to contact owner

## Pact Submission Flow

1. **Owner-linked check**: Run `caw --format json status` and read `owner_linked`. Note the value — it determines whether step 4 requires explicit confirmation.

2. **Dedup check**: `caw --format json pact list --status pending_approval --wallet-id <id>`. If a pending request exists with the same intent, do NOT submit — inform the user and share the existing review link. If the user changed their intent, revoke the old one first (`caw --format json pact revoke <old_pact_id>`), then proceed.

3. **Construct** pact parameters from the user's intent (see [Intent -> Submit Parameter Mapping](#intent---submit-parameter-mapping))

4. **Pre-submit preview** — always present a human-readable summary before submitting. Render durations as calendar units, permissions as plain-English labels, and policy limits as natural language (no raw JSON):

   ```
   📋 Authorization Request

   🎯 Intent:      [one-sentence goal: what asset, what action, which chain, what cadence]

   🔐 Permissions:
      • write:contract_call — execute smart contract transactions
      • read:wallet         — read balances and transaction history
      (list only the permissions actually requested; map to plain-English labels below)

   📜 Policy & Limits:
      • Max per transaction: $550
      • Daily cap (rolling 24h): $600
      • Scope: Base chain · ETH only
      (omit lines that don't apply; if no policy, write "No additional spend limits")

   ⏳ Valid for:   90 days  (until 2026-07-01)
      (if no expiry: "No expiry — active until manually revoked")

   📝 Execution Plan:
      [inline summary drawn from --execution-plan: what will happen, in what order,
       how it stops — 2–4 bullet points; no technical jargon]
   ```

   **Permission label map** (for display only):

   | Raw value | Human label |
   |---|---|
   | `write:transfer` | Execute token transfers |
   | `write:contract_call` | Execute smart contract transactions |
   | `read:wallet` | Read balances and transaction history |
   | `operator` | Execute transfers and contract transactions |
   | `viewer` | Read-only access |

   **After the preview block**, add the appropriate call-to-action:

   - **If `owner_linked = false`** — the request will **auto-activate** without human review. Append a warning and require explicit confirmation before proceeding:

     > ⚠️ **This wallet has no linked owner.** The pact above will activate immediately — no human approval will occur.
     >
     > Confirm submission?

     Only proceed after explicit user confirmation. If the user declines: stop, do not submit.

   - **If `owner_linked = true`** — the request requires owner approval before anything executes. Append:

     > The owner will review this request before it activates. Submitting now…

     Then proceed to submit without waiting for additional user confirmation.

5. **Submit**: `caw --format json pact submit ...`

6. **Communicate**: Tell the user the pact has been submitted and the owner must approve it in the **CAW App** before execution proceeds. Instruct the user to forward the request to the owner. If the owner does not have the app yet, share the download link:

   > ✅ Pact submitted. The owner needs to review and approve it in the **CAW App** before I can proceed.
   >
   > 📱 **Ask the owner to open the CAW App and approve the pending request.**
   >
   > If the owner hasn't installed the app yet, they can download it here:
   > **[Download CAW App](<APP_DOWNLOAD_URL>)**

7. **Track**: Run `caw track --watch &` in the background to receive terminal state notifications (`active`/`rejected`/`completed`/`expired`/`revoked`) delivered via `openclaw agent --deliver`. The `prompt` stored in `--context` controls what the receiving agent does.

8. **On `active`** *(delivered via [Notification])*: Immediately reply to the user that the pact has been approved. Then trigger execution as a **separate background task** via `exec background:true` — never block the notification turn waiting for tx results. See [Background Execution Rule](#background-execution-rule).

9. **On `rejected`** *(delivered via [Notification])*: Tell the user "The owner declined this action." Suggest adjustments if applicable.
## Transfer Quota Exhaustion Fallback

When `caw tx transfer` is denied due to cumulative limit exhaustion (daily/monthly spending cap reached — NOT a per-transaction limit), automatically switch to the pact submission flow:

1. Detect denial with cumulative limit reason (e.g., `rolling_24h`, `rolling_30d` in error response)
2. Inform the user: "The current spending limit has been reached. I'll request a pact for this transfer."
3. Submit a pact with `--permissions write:transfer,read:wallet`, scoped to this specific transfer
4. Wait for owner approval, then execute the transfer under the new pact

> **Do not** trigger this fallback for per-transaction limit denials. For those, follow the standard [error handling flow](./error-handling.md) (suggest reduced amount or ask user to contact owner).

## Lifecycle

See [pact-knowledge.md — Pact Lifecycle](./pact-knowledge.md#pact-lifecycle) for the full state diagram and terminal state descriptions.

Common states:
- `pending_approval`: submitted, awaiting owner decision
- `active`: approved, delegated execution can proceed
- terminal: `rejected`, `completed`, `expired`, `cancelled`

Use `caw --format json pact get <pact_id>` to observe state transitions and current details.

## CLI Command Reference

### `caw pact submit`

Submit a new pact for owner approval. Creates a `PENDING_APPROVAL` request and sends a notification to the owner via CAW App.

**Required flags:**

| Flag | Description |
|---|---|
| `--wallet-id <uuid>` | Target wallet UUID |
| `--intent <text>` | Natural language description of the pact's purpose |

**Optional flags (inline mode):**

| Flag | Default | Description |
|---|---|---|
| `--permissions <list>` | `operator` | Comma-separated permissions granted to the operator. Values: `operator`, `viewer`, `read:wallet`, `write:wallet`, `write:transfer`, `write:contract_call`, `write:manage`. Always choose the **minimum** set needed (see [Least Privilege](#least-privilege-principle)). |
| `--duration <seconds>` | `0` (no expiry) | Pact duration in seconds from activation. Prefer an explicit finite duration unless the user explicitly requests no expiry. |
| `--max-tx <usd>` | -- | Maximum USD value per transaction. Creates an inline transfer limit policy. This is a shortcut; for fine-grained policies use `--spec-file` / `--spec-json`. |
| `--name <text>` | derived from `--intent` | Human-readable name for owner review |
| `--resource-scope <json>` | -- | Resource scope constraints as JSON, e.g. `'{"wallet_id":"<uuid>"}'`. Always bind to the target wallet at minimum. |
| `--execution-plan <text>` | -- | Free-form execution plan in markdown format, shown to the owner during approval review. Use sections like `# Summary`, `# Contract Operations`, `# Risk Controls`, `# Schedule` to help the owner understand the concrete actions. See [pact-knowledge.md](./pact-knowledge.md#execution-plan-structure) |
| `--original-intent <text>` | -- | Raw user input that triggered this request. For single-turn: pass the user message verbatim. For multi-turn: concatenate all user messages directly related to this operation in order, formatted as `"User: <msg1>\nUser: <msg2>"`. Omit unrelated messages |

**Full spec mode (for advanced policy control):**

| Flag | Description |
|---|---|
| `--spec-file <path>` | Path to a JSON file containing the full pact spec (permissions, policies, duration, completion conditions, resource scope, execution_plan). Mutually exclusive with `--spec-json` and inline flags (`--permissions`, `--duration`, `--max-tx`, `--resource-scope`). |
| `--spec-json <json>` | Inline JSON string containing the full pact spec. Mutually exclusive with `--spec-file` and inline flags. |

Use `--spec-file` or `--spec-json` when you need custom policies (allow/deny pairs, chain/token/contract scoping, rolling usage limits). See [pact-knowledge.md](./pact-knowledge.md#policy-construction-patterns) for policy schema and construction patterns.

**Status tracking (pact submit only):** `--context` is **required** for `caw pact submit`. When openclaw notification context is available, pass `channel`, `target`, and `session_id`. **`session_id`**: UUID string identifying the current openclaw conversation session — read it from `openclaw sessions --json --agent <agent>` or equivalent method. If notification context is not available, pass `--context '{}'`. Use: `--context '{"channel":"<channel>", "target":"<target>", "session_id":"<uuid>"}'`
**Example (inline mode):**

```bash
caw --format json pact submit \
  --wallet-id a1b2c3d4-5678-9abc-def0-123456789abc \
  --intent "Execute weekly ETH DCA on Base for 3 months" \
  --permissions operator \
  --duration 7776000 \
  --max-tx 500 \
  --name "Base ETH Weekly DCA" \
  --resource-scope '{"wallet_id":"a1b2c3d4-5678-9abc-def0-123456789abc"}' \
  --execution-plan "# Summary
Weekly DCA: swap ~\$500 USDC to ETH via Uniswap V3 on Base.

# Contract Operations
- Protocol: Uniswap V3 SwapRouter
- Chain: Base
- Contract: 0x2626664c2603336E57B271c5C0b26F421741e481
- Function: exactInputSingle

# Risk Controls
- Max per swap: \$550, Max daily: \$600
- Pre-swap balance check: skip if USDC < \$500

# Schedule
Every Monday, 90 days from activation.

# Exit Conditions
After 12 swaps OR \$6,000 total spent OR 90 days." \
  --context '{"channel":"discord", "target":"1483060020718473359", "session_id":12345}' \```

**Example (full pact spec with policies):**

```bash
caw --format json pact submit \
  --wallet-id a1b2c3d4-5678-9abc-def0-123456789abc \
  --intent "Execute weekly ETH DCA on Base for 3 months" \
  --name "Base ETH Weekly DCA" \
  --spec-file ./pact-dca.json \
  --context '{"channel":"discord", "target":"1483060020718473359", "session_id":12345}' \```

Where `pact-dca.json` contains a full pact spec with policies:

```json
{
  "permissions": ["write:contract_call", "read:wallet"],
  "policies": [
    {
      "name": "dca-uniswap-allow",
      "type": "contract_call",
      "rules": {
        "effect": "allow",
        "when": {
          "chain_in": ["BASE"],
          "target_in": [{ "chain_id": "BASE", "contract_addr": "0x2626664c2603336E57B271c5C0b26F421741e481" }]
        },
        "review_if": { "amount_usd_gt": "500" }
      }
    },
    {
      "name": "dca-uniswap-deny-limits",
      "type": "contract_call",
      "rules": {
        "effect": "deny",
        "when": {
          "chain_in": ["BASE"],
          "target_in": [{ "chain_id": "BASE", "contract_addr": "0x2626664c2603336E57B271c5C0b26F421741e481" }]
        },
        "deny_if": {
          "amount_usd_gt": "550",
          "usage_limits": {
            "rolling_24h": { "amount_usd_gt": "600", "tx_count_gt": 5 }
          }
        }
      }
    }
  ],
  "duration_seconds": 7776000,
  "completion_conditions": [
    { "type": "tx_count", "threshold": "12" },
    { "type": "amount_spent_usd", "threshold": "6000" }
  ],
  "resource_scope": { "wallet_id": "a1b2c3d4-5678-9abc-def0-123456789abc" },
  "execution_plan": "# Summary\nWeekly DCA: swap ~$500 USDC to ETH via Uniswap V3 on Base.\n\n# Contract Operations\n- Protocol: Uniswap V3 SwapRouter\n- Chain: Base\n- Contract: 0x2626664c2603336E57B271c5C0b26F421741e481\n- Function: exactInputSingle\n\n# Risk Controls\n- Max per swap: $550 USD\n- Max daily: $600 USD\n- Pre-swap balance check: skip if USDC < $500\n\n# Schedule\nEvery Monday, 90 days from activation.\n\n# Exit Conditions\nAfter 12 swaps OR $6,000 total spent OR 90 days."
}
```

### `caw pact get <pact-id>`

Get details of a specific pact request. When the request is `pending_approval` and the linked approval has been resolved, this endpoint triggers lazy activation or rejection.

- If approved → returns `status: active` with `api_key`, `delegation_id`, `expires_at`
- If rejected → returns `status: rejected`

The `api_key` field is only visible to the operator principal that submitted the request.

```bash
caw --format json pact get <pact_id>
```

### `caw pact list`

List pacts accessible to the authenticated principal.

| Flag | Default | Description |
|---|---|---|
| `--status <status>` | — | Filter by lifecycle state: `pending_approval`, `active`, `rejected`, `completed`, `expired`, `cancelled` |
| `--wallet-id <uuid>` | — | Filter by wallet UUID |
| `--limit <n>` | `50` | Maximum results (1–200) |
| `--offset <n>` | `0` | Number of results to skip |

```bash
# List all pending pacts for a specific wallet
caw --format json pact list --status pending_approval --wallet-id <wallet_uuid>

# List all active pacts
caw --format json pact list --status active
```

### `caw pact events <pact-id>`

Get the lifecycle event history for a pact. Useful for tracking state transitions, activation timestamps, and completion/revocation reasons.

```bash
caw --format json pact events <pact_id>
```

### `caw pact cancel <pact-id>`

Cancel an active pact. **Owner only.** Cancelling revokes the associated delegation, invalidates the pact-scoped API key, and records a `cancelled` event. This action cannot be undone.

Prompts for confirmation by default. Use `--yes` to skip.

```bash
caw --format json pact cancel <pact_id>
```

## Least Privilege Principle

Every pact request MUST use the minimum access needed for the task. Over-scoped requests increase risk and may be rejected by the owner.

**Permissions**: Choose the narrowest set:

| Task type | Permissions | Rationale |
|---|---|---|
| Read-only monitoring (balance alerts, portfolio tracking) | `viewer` (expands to `read:wallet`) | No writes needed |
| Token transfers only (payroll, invoice payments) | `read:wallet`, `write:transfer` | No contract calls needed |
| Contract calls only (DeFi swaps, staking) | `read:wallet`, `write:contract_call` | No direct transfers needed |
| Both transfers and contract calls | `operator` (shorthand) | Only when both are genuinely needed |

**Policies**: Scope as tightly as possible — restrict `chain_in`, `token_in`, `target_in` to only what the task needs, set `deny_if.amount_usd_gt` and rolling usage limits, use `review_if` for soft thresholds. See [pact-knowledge.md — Policy Construction Patterns](./pact-knowledge.md#policy-construction-patterns) for full schema and examples.

**Duration and completion**: Always set finite bounds:

- Set `--duration` to the shortest time window that covers the task
- Set completion conditions (`tx_count`, `amount_spent_usd`) when the total scope is bounded
- Avoid `0` duration (no expiry) unless the user explicitly requests indefinite access

**Resource scope**: Always bind to the target wallet via `--resource-scope '{"wallet_id":"<uuid>"}'`.

## Intent -> Submit Parameter Mapping

When user intent is fully understood and execution is ready, construct submit arguments:

| Intent component | Flag | Mapping guidance |
|---|---|---|
| Target wallet | `--wallet-id` | Exact wallet UUID |
| Goal description | `--intent` | Normalized goal including asset/protocol/chain/cadence and key risk constraints |
| Operation scope | `--permissions` | Least privilege set (see [Least Privilege](#least-privilege-principle)). Default `operator` only if both transfers and contract calls are needed |
| Time window | `--duration` | Parse explicit time: `30d` -> `2592000`, `3 months` -> `7776000`. Prefer finite duration. |
| Per-transaction budget | `--max-tx` | Per-transaction USD cap if user provided budget constraints (inline policy shortcut) |
| Custom policies | `--spec-file` / `--spec-json` | Use when the task needs chain/token/contract scoping, allow/deny pairs, or rolling usage limits. See [pact-knowledge.md](./pact-knowledge.md#policy-construction-patterns) for patterns. |
| Display name | `--name` | Concise title for owner approval review |
| Resource binding | `--resource-scope` | JSON scope constraints; always bind to wallet |
| Execution plan | `--execution-plan` | Free-form markdown with `# Summary`, `# Contract Operations`, `# Risk Controls`, `# Schedule` sections. Helps owner make informed approval decision |
| Raw user input | `--original-intent` | Pass user's original message(s) verbatim. Single-turn: the triggering message. Multi-turn: concatenate all messages relevant to this operation in order as `"User: <msg1>\nUser: <msg2>"`. Skip unrelated turns |

**Choosing inline vs. full spec:**

| Scenario | Approach |
|---|---|
| Simple budget cap only (max USD per tx) | Use `--max-tx` inline flag |
| Needs chain/token/contract restrictions, rolling limits, or allow+deny pairs | Use `--spec-file` or `--spec-json` with full policies array |
| Complex multi-policy setup with completion conditions | Use `--spec-file` pointing to a JSON file for readability |

**Example mapping:**

> User: "DCA $500/week into ETH on Base for 3 months, max $550 per swap"

```bash
caw --format json pact submit \
  --wallet-id <uuid> \
  --intent "DCA $500/week into ETH on Base for 3 months, max $550 per swap" \
  --permissions write:contract_call,read:wallet \
  --duration 7776000 \
  --max-tx 550 \
  --name "Base ETH Weekly DCA" \
  --resource-scope '{"wallet_id":"<uuid>"}' \
  --context '{"channel":"<channel>", "target":"<target>", "session_id":12345}' \```

Note: `--permissions` uses `write:contract_call,read:wallet` instead of the broader `operator` since DCA swaps only need contract calls, not direct transfers.

## Submission Rules

If delegated execution is required and intent is complete, submit a pact immediately before execution.

**Readiness checklist before submit:**

- [ ] Wallet target is explicit (user confirmed or only one wallet available)
- [ ] No duplicate pending request for this wallet (dedup check in [flow step 1](#pact-submission-flow))
- [ ] Intent is specific and auditable (includes asset, action, chain, constraints)
- [ ] Permissions follow [least privilege](#least-privilege-principle) — only what the task requires
- [ ] Policies scope operations to specific chains, tokens, and/or contracts when applicable
- [ ] Duration is finite (or user explicitly confirmed unlimited)
- [ ] Budget constraints are explicit (per-tx via `--max-tx` or policies, cumulative via completion conditions)
- [ ] `--resource-scope` binds to the target wallet
- [ ] `--name` is concise and describes the task for owner review
- [ ] `--execution-plan` describes concrete actions for multi-step or non-obvious tasks
- [ ] `--original-intent` captures the user's raw input (single message or multi-turn concatenation)

**Do NOT submit if:**

- `owner_linked` is `false` and the user has not yet confirmed (see [flow step 1](#pact-submission-flow))
- The user's intent is ambiguous — ask for clarification first
- The wallet target is unknown — query `caw --format json status` or ask the user
- The operation is a token transfer within default quota — try `caw tx transfer` directly first

## Post-Submission Flow

### Background Execution Rule

**Any execution triggered by a notification — pact status `active`, or any other [Notification] turn — MUST use `exec background:true`. Never synchronously wait for tx results inside the notification turn.**

Why: a notification turn has a short reply window. Blocking it on-chain tx confirmation (which can take minutes) causes timeouts and leaves the conversation unresponsive. The correct pattern is:

1. **Reply immediately** in the notification turn: "Authorization approved — starting execution now."
2. **Trigger execution** via `exec background:true` as a separate task. The background task configures the pact-scoped API key and runs the operations.
3. **Report results** when the background task completes, via a follow-up message or `caw track` notification.

This rule applies to:
- Pact `active` notifications
- Any `[Notification]` turn that needs to perform on-chain writes

### Tracking Approval

After submit, the request enters `pending_approval`. To manually check status:

```bash
caw --format json pact get <pact_id>
```

### Using the Pact-Scoped API Key

When the request becomes `active`, the response includes an `api_key`. The operator uses this key for all subsequent operations under the pact:

```bash
# Pass the pact-scoped API key via flag or env var
export AGENT_WALLET_API_KEY=caw_sk_pact_abc123...
# or pass inline per command:
# caw --api-key caw_sk_pact_abc123... --format json tx call ...

# Execute operations within pact scope
caw --format json tx call --chain BASE --contract 0x... --calldata 0x...
```

All operations are checked against the delegation-scoped policies.

## Handling Outcomes

| Status | Agent action |
|---|---|
| `pending_approval` | Notify user that owner approval is required via the **CAW App**. Ask the user to forward the request to the owner. If the owner doesn't have the app, share the download link: **[Download CAW App](<APP_DOWNLOAD_URL>)** |
| `active` | Proceed with execution within pact scope |
| `rejected` | Surface rejection to user; ask whether to adjust constraints and submit a new pact |
| `cancelled` / `expired` / `completed` | Stop execution; inform user; submit a new pact if continued action is needed |

## Troubleshooting

| Symptom | Cause | Resolution |
|---|---|---|
| `403` on submit | Agent not claimed by an owner, or not an AGENT principal | Run `caw onboard` and ensure the owner has claimed the agent |
| `404` on submit | Wallet not found or not owned by the agent's owner | Verify wallet UUID with `caw --format json status` |
| `422` on submit | Invalid pact spec (policy rules, permissions, or completion conditions malformed) | Check [pact-knowledge.md](./pact-knowledge.md) for schema rules and validation constraints |
| Pact stuck in `pending_approval` | Owner hasn't reviewed in CAW App | Inform user that owner approval is pending |
| `api_key` not in response | Querying principal is not the submitting operator | Only the operator that submitted the request can see the API key |

For pact spec construction details, policy schemas, and validation rules, see [pact-knowledge.md](./pact-knowledge.md).
