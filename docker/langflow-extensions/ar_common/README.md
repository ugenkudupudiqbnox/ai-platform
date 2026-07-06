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

This bundle requires **no credentials** itself. Source-system credentials
(`ZOHO_*`, `FOODICS_API_TOKEN`) are handled by the `ar_tools` bundle via LangFlow
Secret Global Variables.

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
```

## Validate offline

```bash
docker exec langflow python -m lfx extension validate /app/extensions/ar_common
```