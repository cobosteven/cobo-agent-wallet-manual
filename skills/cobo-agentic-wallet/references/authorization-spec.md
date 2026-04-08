# Authorization Spec (Pact Submit/Update Constraints)

This file tracks runtime authorization constraints enforced by the CAW backend.

## Pact Completion Conditions

- `POST /api/v1/pacts/submit`: `spec.completion_conditions` is required and must contain at least one item.
- `PATCH /api/v1/pacts/{pact_id}/completion-conditions`: `completion_conditions` is required and must contain at least one item.

## Pact Policies

- `POST /api/v1/pacts/submit`: `spec.policies` is required and must contain at least one item.

For broader pact guidance and policy authoring details, see `pact.md`.
