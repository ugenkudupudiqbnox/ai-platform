# Session Notes — 2026-07-08

Cosmic AR Agent: verify deployed fixes, close the remaining live blockers, and
surface the subflow's computed numbers in the supervisor response.

## What we accomplished

1. **Verified the prior session's fixes are deployed and working.** Confirmed
   the deployed `supervisor.json` embedded code is byte-identical to the fixed
   on-disk `supervisor.py` (commits `f2719d1` V1-RESUME, `8ee6bd6`
   V1-FLOW-TWEAK-DATA + V1-RUNFLOW-TOOL-INPUT). Instrumented `_call_tool` and
   ran the **real REST path** to prove `_extract_runflow_component` returns the
   original `RunFlowComponent` and `flow_tweak_data` reaches the subflow's
   `ChatInput` — so the async-bridge + original-component workaround functions
   in production, not just in-process.

2. **Found + fixed `V1-PAYLOAD-EXTRACT`** (commit `b1f7157`). The prior
   "AR_OK end-to-end" verification was misleading: it fed `json.dumps(payload)`
   (pure JSON) straight to the tool, bypassing `_finalize_envelope`. A real
   adapter user sends **NL + embedded JSON** (the classifier needs NL
   keywords), but every JSON subflow `json.loads` its `ChatInput` directly and
   rejected the `"Calculate"` prefix → `AR_UNEXPECTED "payload JSON parse
   error"`. Added `_extract_json_object` (brace-balanced, string-literal/escape
   aware) + intent-aware `_subflow_input` in `supervisor.py`; wired into
   `_node_invoke`. `ar_approval` decision replies pass through verbatim;
   no-JSON → raw message (subflow returns its own `AR_VALIDATION`).

3. **Found + fixed `V1-RESULT-SURFACE`** (commit `ee50b85`). The supervisor's
   `AR_OK` envelope had `data: null` — `_finalize_envelope` built `base` with
   no `data`, and `_node_invoke` only lifted `totals`/`audit_refs` to
   top-level, so the subflow's computed figures vanished. Added additive
   `AgentState.result_data`; `_node_invoke` stores the subflow's §14 `data`;
   `_finalize_envelope` sets `data = {"result": state.result_data} if
   state.result_data else {}`. Live-verified: `ar_calculation` now returns the
   9 figures at `data.result.calculation_result.totals`.

4. **All four V1-* fixes deployed and live-verified through the real REST
   path**, regression-swept across the approval tier and read-only subflows.

## Key decisions made

- **D1 — Extract JSON in the supervisor, not per-subflow.** All 9 JSON
  subflows are strict pure-JSON consumers (`json.loads(input_value)` directly).
  Extraction belongs once in the supervisor (`_subflow_input`), intent-aware
  (`ar_approval` is an NL decision reply → passthrough), with a no-JSON
  fallback to the raw message so the subflow returns its own graceful
  `AR_VALIDATION` rather than a tool-level error.
- **D2 — `data.result` (nested), not flat `data`.** User-confirmed via
  AskUserQuestion. `execution-summary.schema.json` reserves `data` for a
  supervisor-level `ExecutionSummary` (deferred `V1-ENVELOPE-META`). Nesting
  the subflow payload under `data.result` composes with the v2
  `data.execution_summary` instead of conflicting with it.
- **D3 — `pending_approval`/`awaiting_approval` keep `data = {action, tier}`.**
  Do not surface `data.result` on the pending path — the approval contract
  (§19) is about the proposed action, not a result. Unchanged.
- **D4 — `failed` path carries `data.result`** (the subflow's
  `validation_report`/`exception_report`). Useful debugging surface; no
  regression.
- **D5 — No new supervisor self-test.** The data-carry is pure dict plumbing
  (no novel logic); no `supervisor.selftest.sh` harness exists (would need a
  heavyweight lfx/langgraph mock). Covered by live end-to-end verification.
- **D6 — Do not pre-commit the full `data.execution_summary` reshape.** Keep
  `V1-ENVELOPE-META` deferred to v2 (coordinated adapter + self-test +
  schema change). Only the additive `data.result` surfacing was done now.

## Patterns established

- **P1 — Live REST run is ground truth, not in-process warm-cache tests.**
  `docker exec aiplatform-langflow-1 … curl -s --compressed -X POST
  http://localhost:7860/api/v1/run/<UUID>` exercises the real
  `_finalize_envelope`. In-process `tool.ainvoke` tests bypass it and can mask
  defects (this is exactly how V1-PAYLOAD-EXTRACT went unnoticed).
- **P2 — Instrumented deploy for ground truth.** Add temporary diagnostics to
  `_call_tool` (write a JSONL line to the container's `/tmp`), re-embed, PATCH,
  run, read — then **revert the diagnostics** before the final commit.
- **P3 — Deploy mechanism (no UUID churn).** Edit repo `.py` → re-embed into
  `supervisor.json` `template.code.value` byte-identical
  (`json.dump(indent=2, ensure_ascii=False)`) → in-place PATCH
  `/api/v1/flows/<UUID>` (UUID unchanged → **no adapter repoint/recreate**).
- **P4 — Restart rule (critical).** **Embedded** component code
  (`supervisor.py`'s embedded copy) is recompiled by lfx **per run → no
  restart**. **Imported** modules (`agent_state.py`, `envelope.py`,
  `idempotency.py`, other `ar_common` modules) are cached in the long-running
  LangFlow process's `sys.modules` → **require `docker restart
  aiplatform-langflow-1`** after PATCH. (Hit this: `'AgentState' object has no
  attribute 'result_data'` until restart.)
- **P5 — Embed-sync invariant.** `supervisor.json` `template.code.value` must
  be byte-identical to the on-disk `supervisor.py`; re-verify after every
  edit.
- **P6 — Regression sweep after any supervisor change.** `ar_calculation`
  (AR_OK + figures), `ar_audit` (AR_OK read-only), `ar_issue_invoice`
  (`pending_approval` — §19 gate intact, `data={action,tier}`),
  `ar_kitchen_revenue` (error, own `validation_report` surfaces, no
  parse-error regression).

## Next steps

- **N1 — `V1-ENVELOPE-META` (v2, the remaining gap).** Make the supervisor
  envelope fully §14-conformant: move top-level run-metadata (`flow_id`,
  `tenant`, `checkpoint_id`, `audit_refs`, `totals`, `started_at`,
  `ended_at`, `contract_version`) under `data.execution_summary` per
  `execution-summary.schema.json`, trimming the envelope top level to §14's
  6 keys. **Coordinated change** — the adapter reads `checkpoint_id` from the
  top level today (`adapter.py` pending-approval path), and self-tests/schema
  must move with it. `data.result` (done) composes under the new `data`.
- **N2 — LangGraph §19 resume path.** `Command(resume=approval_ref)` live
  approval resume is still build-phase (the deferred half of `V1-RESUME`).
- **N3 — Classifier robustness.** `ar_issue_invoice` keywords are strict
  substrings (`"issue invoice"`, not `"issue an invoice"`) — intentional but
  narrow. Consider lenient matching or document exact user phrases.
- **N4 — Supervisor self-test harness.** Add at least pure-logic tests for
  `_extract_json_object`, `_subflow_input`, `_tools_by_name`,
  `classify_intent` (no lfx/langgraph needed) to guard against regressions.
- **N5 — Build-phase caveats (unchanged).** Real Zoho/Foodics transports
  (`V1-STUB`), PDF/Excel binaries (`V1-RENDER`), Postgres/Langfuse audit
  persistence (`V1-NOPERSIST`), `SecretStrInput` creds (`V1-NO-SECRETSTR`),
  cross-subflow audit auto-accumulation (`V1-NO-AUTOAUDIT`).

## Blockers

- **None blocking.** All four V1-* fixes (FLOW-TWEAK-DATA, RUNFLOW-TOOL-INPUT,
  PAYLOAD-EXTRACT, RESULT-SURFACE) are deployed and live-verified. The stack
  is operational: subflows run end-to-end and surface their computed results.
- **Deferred (by prior user decision, not blockers):** full `V1-ENVELOPE-META`
  §14 conformance + §19 live resume — scoped to v2/build-phase.

## Commits this session

- `8ee6bd6` — `fix(cosmic-ar): invoke async RunFlow tools via sync-bridge +
  original-component input workaround` (V1-FLOW-TWEAK-DATA +
  V1-RUNFLOW-TOOL-INPUT — committed at session start, verified live here)
- `b1f7157` — `fix(cosmic-ar): extract JSON payload from NL chat message
  before invoking subflow` (V1-PAYLOAD-EXTRACT)
- `ee50b85` — `fix(cosmic-ar): surface subflow result under data.result in the
  supervisor envelope` (V1-RESULT-SURFACE)

All pushed to `origin/main` (fast-forward). `graphify update .` run after each.

## Real Zoho Books + Foodics sandbox transports (build-phase, this session)

**Goal:** make `ar_issue_invoice` + `ar_foodics_processing` perform real HTTP
against vendor sandboxes (the user asked to deploy + test in Zoho/Foodics
sandboxes and how to set up the API keys). User chose **Zoho + Foodics together**
and **LangFlow Secret Global Variables** for credential storage.

**Built (in `ar_common`, pure-Python transports — no lfx import, offline-testable):**
- `vendor_secrets.py` — `read_secret(component, name, default)` resolves a named
  Secret Global Variable via the subflow component's `variables(name, name)`
  (lfx sync wrapper; the component carries `user_id`, the DB-lookup key), unwraps
  `pydantic.SecretStr` via `get_secret_value()`, falls back to `os.getenv` then
  `default`. Returns `None` when absent → fail-safe stays.
- `zoho_transport.py` — `RealZoho(creds)`: OAuth refresh-on-401 + POST
  `/invoices` + DELETE `/invoices/{id}`, `organization_id` query param (mirrors
  `ap_tools`); returns the `StubZohoUpload` dict shape (`ok, http_status, code,
  zoho_id, zoho_ref, duplicate, transient`); maps 201/`code:0`→`AR_OK`,
  duplicate-invoice-number→`AR_DUPLICATE`, 429/5xx→transient, hard 4xx→
  `AR_VALIDATION`/`AR_FORBIDDEN`/`AR_NOT_FOUND`/`AR_UNEXPECTED`.
- `foodics_transport.py` — `RealFoodics(creds)`: OAuth 2.0 client-id/secret/
  refresh → 14-day Bearer + `X-Business`; `list_orders`/`list_order_items`/
  `list_order_payments` → Laravel pagination; normalizes rows to the canonical
  column names `_header_map` expects; returns a §14 envelope string; raises
  transiently (`_TransientFoodicsError` with int `.code`, or `requests`
  Connection/Timeout) so the §10 loop owns retry; hard 4xx → error envelope.

**Wired (lazy, in the subflow components' `run()`):**
- Zoho: `set_transport(RealZoho(creds))` when all four core creds present; else
  keep `StubZohoUpload` (no reset — preserves self-test stubs).
- Foodics: new `set_foodics_creds(creds)` module-global seam (mirrors Zoho's
  `set_transport`); `_make_foodics_fetcher()` returns `RealFoodics(creds)` when
  configured else `None` (drops the broken `from components.ar_tools.foodics_ar
  import FoodicsARTool` cross-bundle import). `run()` resets to `None` on the
  no-creds path so a prior run's creds never leak.

**Deployed:** re-embedded both edited `.py` byte-identical into
`ar_issue_invoice.json` + `ar_foodics_processing.json` → in-place PATCH by live
UUID (`b5b49e24…` / `87d38266…`, unchanged → no adapter repoint) →
`docker restart aiplatform-langflow-1` (new imported modules cached in
`sys.modules`). DB-embedded code verified (markers + lengths match disk).

**Verified:** `make test` green (22 suites / 1776 checks); egress OK (TCP:443 to
`accounts.zoho.com`/`www.zohoapis.com`/`api.foodics.com`/`console-sandbox.foodics.com`);
SSRF irrelevant (transports call `requests` directly, bypassing lfx's
`validate_url_for_ssrf`; `LANGFLOW_SSRF_ALLOWED_HOSTS` is a bypass-list, not a
restrict-to-only gate); no-creds regression sweep via the real supervisor REST
path — `ar_foodics_processing` routes → §19 `pending_approval` (clean),
`ar_calculation` → `AR_OK` (supervisor + RunFlow path healthy post-restart).

**Key insight — SSRF:** the transports use raw `requests`, so lfx's SSRF layer
does not gate them. `LANGFLOW_SSRF_ALLOWED_HOSTS=10.14.210.7` is a *bypass-list*
(`is_host_allowed` skips the blocked-range check for listed private hosts), not a
restrict-to-only gate — public vendor hosts are allowed regardless. The
environment.md + build-phase wiring summary were corrected (they previously said
the vendor hosts "must be listed").

**Next / blocker:** live real-vendor calls are gated on the operator creating the
Secret Global Variables in the LangFlow UI (the user's "how to set up the API
keys" question → answered in `cosmic-ar/docs/environment.md` with per-vendor
obtain steps + UI creation steps + verify commands). **Infrasys still absent**
(no flow/transport; Shiji partner endorsement is long-lead — start the email).