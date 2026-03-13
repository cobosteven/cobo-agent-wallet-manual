# Policy Management

Create, inspect, test, and troubleshoot policies.

## List policies by scope

```bash
# All delegation-scoped policies (default)
caw --format json policy list

# Global policies
caw --format json policy list --scope global

# Policies for a specific delegation
caw --format json policy list --scope delegation --delegation-id <delegation_id>
```

## Inspect a policy

```bash
caw --format json policy get <policy_id>
```

## Dry-run a policy check

Test whether a transfer would be allowed without executing it:

```bash
caw --format json policy dry-run <wallet_id> \
  --operation-type transfer \
  --amount 100 --chain-id BASE \
  --token-id USDC --dst-addr 0x1234...abcd
```

## View delegation details

```bash
# List all delegations received
caw --format json delegation received

# Get specific delegation
caw --format json delegation get <delegation_id>
```

## Troubleshooting policy denials

1. Check the denial `suggestion` field for guidance
2. Dry-run with adjusted parameters to verify
3. If the policy itself needs changing, the owner must update it via the Web Console