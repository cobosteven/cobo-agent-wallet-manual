# Pact automation

Use this guide when the user asks to grant an operator agent scoped, time-bounded permissions and run tasks under those constraints.

For PactSpec schema details, policy construction patterns, and validation rules, see [pact-knowledge.md](./pact-knowledge.md).

## When to use

- User asks for delegated automation (DCA, recurring swaps, rule-based execution).
- User mentions `pact`, delegated API key, approval-based operator authorization, or long-running agent tasks.

## Core model

- A Pact defines **what is allowed** (`permissions`, `policies`, `resource_scope`, lifetime).
- The agent scheduler defines **when to run** (interval/cron/checkpoints).
- In the simplified protocol, the flow is:
  1) operator submits pact
  2) owner approves/rejects in CAW App
  3) `GET /pacts/{id}` triggers lazy activation

## Submit a pact

Prefer full spec files for repeatability:

```bash
caw --format json pact submit \
  --wallet-id "<wallet_uuid>" \
  --intent "Weekly DCA: buy 100 USDC of ETH on Base every Monday" \
  --spec-file "./pact-dca.json" \
  --wait
```

Or use quick inline flags:

```bash
caw --format json pact submit \
  --wallet-id "<wallet_uuid>" \
  --intent "Automate bounded strategy execution" \
  --permissions operator \
  --duration 2592000 \
  --max-tx 100 \
  --wait
```

### PactSpec template

```json
{
  "permissions": ["operator"],
  "policies": [
    {
      "name": "transfer-allow",
      "type": "transfer",
      "rules": {
        "effect": "allow",
        "when": {
          "chain_in": ["BASE"]
        }
      }
    },
    {
      "name": "transfer-deny-limits",
      "type": "transfer",
      "rules": {
        "effect": "deny",
        "when": {
          "chain_in": ["BASE"]
        },
        "deny_if": {
          "amount_usd_gt": "100",
          "usage_limits": {
            "rolling_24h": { "amount_usd_gt": "500" }
          }
        }
      }
    }
  ],
  "duration_seconds": 2592000,
  "completion_conditions": [
    { "type": "manual" }
  ],
  "resource_scope": {
    "wallet_id": "00000000-0000-0000-0000-000000000000"
  },
  "program": "Weekly DCA: every Monday 09:00 UTC buy 100 USDC of ETH on Base."
}
```

See [pact-knowledge.md — Policy Construction Patterns](./pact-knowledge.md#policy-construction-patterns) for more examples.

## Scheduler contract (required for multi-pact automation)

When building an autonomous operator, run a background scheduler. Pact Service enforces authorization boundaries, but does not dispatch jobs for you.

### Required loop

Every `N` seconds (recommended default: `10s`):

1. Pull active pacts:
   ```bash
   caw --format json pact list --status active
   ```
2. Reconcile local jobs with server truth:
   - new active pact -> create/start local job
   - missing from active list -> stop local job
3. Before each on-chain action, re-check:
   ```bash
   caw --format json pact status "<pact_id>"
   ```
   Continue only when status is `active`.

### Local job states

- `DISCOVERED`: found from `pact list --status active`
- `RUNNING`: task loop is executing
- `PAUSED`: transient failure, retry allowed
- `STOPPED`: pact became `revoked`/`expired`/`completed`/`rejected`

Persist `pact_id`, checkpoints, and last idempotency key so process restarts do not lose state.

## Status and lifecycle operations

```bash
caw --format json pact list --status pending_approval
caw --format json pact status "<pact_id>"
caw --format json pact show "<pact_id>"
caw --format json pact events "<pact_id>"
caw --format json pact revoke "<pact_id>" --confirm
```

Terminal statuses (see [pact-knowledge.md — Lifecycle States](./pact-knowledge.md#lifecycle-states) for full diagram):

- `active`: ready to execute
- `rejected`: owner rejected approval
- `expired`: pact lifetime ended or approval expired
- `completed`: completion condition met
- `revoked`: owner revoked; stop immediately

## Safety and reliability rules

- Always use minimum permissions and explicit policies.
- Set finite `duration_seconds` unless user explicitly requests no expiry.
- Use deterministic request IDs on all transactions to avoid duplicates on retries.
- Treat server status as source of truth; local schedule must not override `revoked`/`expired`.
