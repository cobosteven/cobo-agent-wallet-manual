# Recipes

Common usage scenarios and workflows. Each recipe below covers a specific operation. For detailed, multi-step scenarios see the linked files.

## Transfer tokens

```bash
caw --format json tx transfer <wallet_uuid> \
  --to 0x1234...abcd --token USDC --amount 10 --chain BASE \
  --request-id pay-invoice-1001
```

## Check wallet balance

```bash
caw --format json wallet balance <wallet_uuid>
```

## List recent transactions

```bash
caw --format json tx list <wallet_uuid> --limit 20
```

## Estimate fee before transfer

```bash
caw --format json tx estimate-transfer-fee <wallet_uuid> \
  --to 0x1234...abcd --token USDC --amount 10 --chain BASE
```

## Contract call

```bash
caw --format json tx call <wallet_uuid> \
  --contract 0xContractAddr --calldata 0x... --chain ETH
```

## Handle policy denial

When denied, check the `suggestion` field and retry with adjusted parameters.

## Monitor pending approvals

When a transaction returns HTTP 202, poll with `caw pending get <operation_id>`.

## Detailed Scenarios

- [Policy Management](./recipes/policy-management.md) — Create, test, and troubleshoot policies
- [Error Handling](./recipes/error-handling.md) — Common errors, policy denials, and recovery patterns