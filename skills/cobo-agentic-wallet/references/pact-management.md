# Pact Management

Use pact when the task needs owner-approved delegated execution before the agent acts.

## When to use pact

Typical scenarios:
- recurring strategy execution (DCA, periodic rebalance)
- multi-step automation with risk bounds
- temporary delegated access with explicit expiry

**Do NOT use pact for:**
- one-off transfers (use `caw tx transfer` directly)
- read-only queries (use `caw wallet balance`, `caw tx list`, etc.)
- operations where no delegation is needed

## Lifecycle

Common lifecycle states:
- `pending_approval`: submitted and waiting for owner decision
- `active`: approved and activated; delegated execution can proceed
- terminal states: `rejected`, `completed`, `expired`, `cancelled`

Use `caw --format json pact get <pact_id>` to observe state transitions and current details.

## CLI Command Reference

### `caw pact submit`

Submit a new pact for owner approval. Creates a `PENDING_APPROVAL` pact and sends a notification to the owner via CAW App.

**Required flags:**

| Flag | Description |
|---|---|
| `--wallet-id <uuid>` | Target wallet UUID |
| `--intent <text>` | Natural language description of the pact's purpose |

**Optional flags:**

| Flag | Default | Description |
|---|---|---|
| `--permissions <list>` | `operator` | Comma-separated permissions granted to the operator. Values: `operator`, `viewer`, `read:wallet`, `write:wallet`, `write:transfer`, `write:contract_call`, `write:manage` |
| `--duration <seconds>` | `0` (no expiry) | Pact duration in seconds from activation |
| `--max-tx <usd>` | — | Maximum USD value per transaction. Creates an inline transfer limit policy |
| `--name <text>` | derived from `--intent` | Human-readable pact name for owner review |
| `--resource-scope <json>` | -- | Resource scope constraints as JSON, e.g. `'{"wallet_id":"<uuid>"}'` |
| `--program <text>` | -- | Free-form execution plan in markdown format, shown to the owner during approval review. Use sections like `# Summary`, `# Contract Operations`, `# Risk Controls`, `# Schedule` to help the owner understand the concrete actions. See [pact-knowledge.md](./pact-knowledge.md#program-structure) |

**Example:**

```bash
caw --format json pact submit \
  --wallet-id a1b2c3d4-5678-9abc-def0-123456789abc \
  --intent "Execute weekly ETH DCA on Base for 3 months" \
  --permissions operator \
  --duration 7776000 \
  --max-tx 500 \
  --name "Base ETH Weekly DCA" \
  --resource-scope '{"wallet_id":"a1b2c3d4-5678-9abc-def0-123456789abc"}' \
  --program "# Summary
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
After 12 swaps OR \$6,000 total spent OR 90 days."
```

### `caw pact get <pact-id>`

Get details of a specific pact. When the pact is `pending_approval` and the linked approval has been resolved, this endpoint triggers lazy activation or rejection.

- If approved → returns `status: active` with `api_key`, `delegation_id`, `expires_at`
- If rejected → returns `status: rejected`

The `api_key` field is only visible to the operator principal that submitted the pact.

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

## Intent → Submit Parameter Mapping

When user intent is fully understood and execution is ready, construct submit arguments:

| Intent component | Flag | Mapping guidance |
|---|---|---|
| Target wallet | `--wallet-id` | Exact wallet UUID |
| Goal description | `--intent` | Normalized goal including asset/protocol/chain/cadence and key risk constraints |
| Operation scope | `--permissions` | Least privilege set (default `operator`). Use `viewer` if only reads are needed |
| Time window | `--duration` | Parse explicit time: `30d` → `2592000`, `3 months` → `7776000` |
| Per-transaction budget | `--max-tx` | Per-transaction USD cap if user provided budget constraints |
| Display name | `--name` | Concise title for owner approval review |
| Resource binding | `--resource-scope` | JSON scope constraints; at minimum bind to wallet |
| Execution plan | `--program` | Free-form markdown with `# Summary`, `# Contract Operations`, `# Risk Controls`, `# Schedule` sections. Helps owner make informed approval decision |

**Example mapping:**

> User: "DCA $500/week into ETH on Base for 3 months, max $550 per swap"

```bash
caw --format json pact submit \
  --wallet-id <uuid> \
  --intent "DCA $500/week into ETH on Base for 3 months, max $550 per swap" \
  --permissions operator \
  --duration 7776000 \
  --max-tx 550 \
  --name "Base ETH Weekly DCA"
```

## Submission Rules

If delegated execution is required and intent is complete, submit pact immediately before execution.

**Readiness checklist before submit:**

- [ ] Wallet target is explicit (user confirmed or only one wallet available)
- [ ] Intent is specific and auditable (includes asset, action, chain, constraints)
- [ ] Permissions are minimally scoped
- [ ] Duration and budget constraints are explicit (or user-confirmed as unlimited)
- [ ] `--name` is concise and describes the task for owner review

**Do NOT submit if:**

- The user's intent is ambiguous — ask for clarification first
- The wallet target is unknown — query `caw --format json status` or ask the user
- No delegated execution is needed (one-off transfers use `caw tx transfer` directly)

## Post-Submission Flow

### Polling for Approval

After submit, the pact enters `pending_approval`. Poll with `caw pact get <pact_id>` until the status changes:

```bash
caw --format json pact get <pact_id>
```

### Using the Pact-Scoped API Key

When the pact becomes `active`, the response includes an `api_key`. The operator uses this key for all subsequent operations under the pact:

```bash
# Configure the pact API key
caw profile set --api-key caw_sk_pact_abc123...

# Execute operations within pact scope
caw --format json tx call --chain BASE --contract 0x... --calldata 0x...
```

All operations are checked against the pact's delegation-scoped policies.

## Handling Outcomes

| Status | Agent action |
|---|---|
| `pending_approval` | Notify user that owner approval is required; optionally poll periodically |
| `active` | Proceed with execution within pact scope |
| `rejected` | Surface rejection to user; ask whether to adjust constraints and submit a new pact |
| `cancelled` / `expired` / `completed` | Stop execution; inform user; request a new pact if continued action is needed |

## Troubleshooting

| Symptom | Cause | Resolution |
|---|---|---|
| `403` on submit | Agent not claimed by an owner, or not an AGENT principal | Run `caw onboard` and ensure the owner has claimed the agent |
| `404` on submit | Wallet not found or not owned by the agent's owner | Verify wallet UUID with `caw --format json status` |
| `422` on submit | Invalid PactSpec (policy rules, permissions, or completion conditions malformed) | Check [pact-knowledge.md](./pact-knowledge.md) for schema rules and validation constraints |
| Pact stuck in `pending_approval` | Owner hasn't reviewed in CAW App | Inform user that owner approval is pending |
| `api_key` not in response | Querying principal is not the submitting operator | Only the operator that submitted the pact can see the API key |

For PactSpec construction details, policy schemas, and validation rules, see [pact-knowledge.md](./pact-knowledge.md).
