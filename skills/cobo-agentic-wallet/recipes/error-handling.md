# Error Handling

Common errors, policy denials, and recovery patterns.

## Policy denial (403)

The response includes structured fields:

```json
{
  "error": {
    "code": "TRANSFER_LIMIT_EXCEEDED",
    "reason": "max_per_tx",
    "details": {"limit_value": "100"},
    "suggestion": "Try amount <= 100"
  }
}
```

**Recovery:** Use the `suggestion` field to retry with adjusted parameters.

## Validation error (422)

Missing or invalid parameters. The response includes field-level details:

```json
{
  "success": false,
  "error": {
    "detail": [{"loc": ["body", "amount"], "msg": "field required", "type": "missing"}]
  }
}
```

**Recovery:** Check the `loc` and `msg` fields to fix the request.

## Pending approval (202)

Transaction requires owner approval before execution.

```bash
# Poll the pending operation
caw --format json pending get <operation_id>
```

**Recovery:** Wait for the owner to approve/reject in the Web Console, then check the transaction status.

## Insufficient balance

Transfer fails because the wallet lacks sufficient funds.

**Recovery:** Check balance with `caw wallet balance <wallet_uuid>`, then fund the wallet or reduce the amount.

## Non-zero exit code

Any `caw` command returning a non-zero exit code indicates failure. Always check stdout/stderr for error details before retrying.