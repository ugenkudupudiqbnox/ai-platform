# ADR 0001 — Custom LangGraph StateGraph supervisor + Postgres-backed checkpointer

- **Status:** Accepted
- **Date:** 2026-07-06
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none

## Context

The Cosmic AR Agent needs (per the project brief): one Supervisor Agent, nine
reusable LangFlow subflows, explicit LangGraph state, and a checkpoint
architecture. LangFlow 1.10.1 ships no built-in "Supervisor" node; its `Agent`
node uses langgraph internally but exposes state as implicit session message
history, not the typed `AgentState` the constitution §8 prescribes, and offers
no durable, §11-compliant checkpointer. LangGraph 1.2.6 (with `StateGraph` and
`MemorySaver`) is bundled in the `langflowai/langflow:1.10.1` image venv, so it
is available to a custom `lfx` Component without new dependencies.

See [architecture](../../../docs/cosmic-ar-architecture.md) §2/§3/§5/§11 and
[constitution](../../../docs/cosmic-ar-constitution.md) §8/§11.

## Decision

1. The supervisor is a custom `lfx` Component (`SupervisorAgentComponent`) that
   builds an explicit LangGraph `StateGraph[AgentState]` with a typed dataclass
   state and a real LangGraph checkpointer; the nine subflows attach as
   LangChain tools via the built-in **Flow as Tool** node.
2. The checkpointer is Postgres-backed: a thin custom `BaseCheckpointSaver` over
   SQLAlchemy into a least-privilege `ar_agent` DB + `ar_checkpoints` table
   (durable across restart/upgrade). In-process `MemorySaver` is the degraded
   fallback if the DB is unavailable at boot.

## Consequences

- Positive: explicit, typed, immutable `AgentState` (§8); durable resume that
  does not rely on Langfuse spans (§11 caveat — `LANGFLOW_DEACTIVATE_TRACING=true`);
  the nine subflows stay pure, reusable LangFlow flows.
- Negative: a custom component to maintain; a new Postgres DB to provision.
- Risks: `langgraph-checkpoint-postgres` is not confirmed in the image, hence
  the custom SQLAlchemy saver. Mitigation: at build phase, verify and prefer
  baking `langgraph-checkpoint-postgres` into `docker/langflow/Dockerfile`.
- Build-phase follow-ups: implement the `StateGraph` wiring; provision the
  `ar_agent` DB (`docker/postgres/init/02-ar-agent-db.sh` + `docker-compose.yml`
  env); wire `AR_AGENT_DB_*` onto the `langflow` service; extend
  `scripts/gen-secrets.sh` for `AR_AGENT_DB_PASSWORD`.