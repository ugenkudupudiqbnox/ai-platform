# AR Common — LangFlow Extension Bundle

Cross-cutting shared components for the **Cosmic AR Agent**. See
[`docs/cosmic-ar-architecture.md`](../../../docs/cosmic-ar-architecture.md) for
the design and [`docs/cosmic-ar-constitution.md`](../../../docs/cosmic-ar-constitution.md)
for the binding standards (each component is labelled with its §).

## What this bundle exposes

| Component | File | § | Role |
|-----------|------|---|------|
| `SupervisorAgentComponent` | `components/ar_common/supervisor.py` | §8 | Owns the LangGraph `StateGraph[AgentState]` + checkpointer; the only stateful node |
| `JsonEnvelopeComponent` | `components/ar_common/envelope.py` | §14 | Canonical `{status, code, data, error, trace_id, approval_ref}` envelope |
| `ApprovalGateComponent` | `components/ar_common/approval_gate.py` | §19 | Human-approval gate; returns `pending_approval` |
| `IdempotencyKeyComponent` | `components/ar_common/idempotency.py` | §10 | Derives/replays idempotency keys for financial POSTs |
| `CheckpointComponent` | `components/ar_common/checkpoint.py` | §11 | Saves/loads `AgentState` via a Postgres-backed checkpointer |
| `AuditRecordComponent` | `components/ar_common/audit.py` | §13 | Writes the immutable audit record (actor, action, before/after, approval_ref) |

It also defines the typed **state schema** in `components/ar_common/agent_state.py`
(`AgentState` + `Approval` dataclasses, §8) imported by the supervisor.

> **Scaffold only.** Every component is a valid, importable `lfx` Component
> skeleton whose output method returns a placeholder `Message`. Business logic
> (LangGraph wiring, SQLAlchemy saver, OAuth refresh, matching) is filled in at
> the build phase. No business logic is implemented here.

## Credentials

This bundle hosts the AR subflows' **real vendor transports**
(`zoho_transport.RealZoho`, `foodics_transport.RealFoodics`) plus the shared
`vendor_secrets` resolver. The subflow components resolve source-system
credentials (`ZOHO_*`, `FOODICS_*`) **by name** from LangFlow Secret Global
Variables at runtime via `vendor_secrets.read_secret(component, name)` (the
component carries `user_id`; no `SecretStrInput` is added to the subflows → no
flow-JSON surgery). When a required cred is absent, each transport keeps its
fail-safe (Zoho → `StubZohoUpload`; Foodics → files / `AR_NOT_IMPLEMENTED`), so
the bundle imports and the flows run offline with **no credentials**. Foodics is
OAuth 2.0 (`FOODICS_CLIENT_ID`/`CLIENT_SECRET`/`REFRESH_TOKEN`/`BUSINESS_ID`) —
the obsolete static `FOODICS_API_TOKEN` is not used. See
[`cosmic-ar/docs/environment.md`](../../../cosmic-ar/docs/environment.md) for the
full credential-setup guide.

## Layout

```
ar_common/                      # inline-bundle dir MUST be snake_case (bundle-name pattern)
  extension.json                # v1 Extension manifest (bundle = ar_common; id = ar-common)
  pyproject.toml                # pip metadata + langflow.extension entry-point
  components/ar_common/
    agent_state.py              # AgentState + Approval dataclasses (§8)
    supervisor.py               # SupervisorAgentComponent
    envelope.py                 # JsonEnvelopeComponent
    approval_gate.py            # ApprovalGateComponent
    idempotency.py              # IdempotencyKeyComponent
    checkpoint.py               # CheckpointComponent (+ BaseCheckpointSaver outline)
    audit.py                    # AuditRecordComponent
    vendor_secrets.py           # Secret Global Variable resolver (by name; §16)
    zoho_transport.py           # RealZoho — real Zoho Books POST/DELETE (build-phase)
    foodics_transport.py        # RealFoodics — real Foodics OAuth list ops (build-phase)
    zoho_upload_flow.py         # ar_issue_invoice subflow (embeds RealZoho when creds present)
    foodics_processing.py       # ar_foodics_processing subflow (embeds RealFoodics when creds present)
    …                           # the other AR subflow components (calculation, audit, …)
```

## Validate offline

```bash
docker exec langflow python -m lfx extension validate /app/extensions/ar_common
```