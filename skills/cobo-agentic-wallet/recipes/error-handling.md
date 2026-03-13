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

## TSS Node / Onboarding errors

### `invalid node ID, please bind your TSS Node to application first`

TSS Node connected to the wrong environment. Check `--tss-env` parameter matches the setup token's environment (sandbox/dev).

**Recovery:** Stop TSS Node, clean up state (see SKILL.md Reset/Cleanup), re-run `onboard provision` with the correct `--tss-env`.

### `Timed out waiting for wallet activation`

Two possible causes:
1. `--tss-env` mismatch — the TSS Node is talking to the wrong backend
2. Wallet activation requires owner approval in the Human App

**Recovery:** Verify `--tss-env` is correct. If it is, ask the owner to approve the wallet in the Human App.

## Non-zero exit code

Any `caw` command returning a non-zero exit code indicates failure. Always check stdout/stderr for error details before retrying.
