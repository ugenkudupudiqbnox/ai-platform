# Session Notes — 2026-07-08 (Real Zoho Books + Foodics sandbox transports)

A focused work session: build, wire, deploy, and verify the real vendor
sandbox transports for the two vendor-touching AR subflows (`ar_issue_invoice`,
`ar_foodics_processing`), so they perform real HTTP against Zoho Books + Foodics
sandboxes instead of stubs. User chose **Zoho + Foodics together** and **LangFlow
Secret Global Variables** for credential storage (confirmed via AskUserQuestion;
approved plan at `/root/.claude/plans/woolly-wobbling-penguin.md`).

> This complements the earlier 2026-07-08 V1-* fix notes
> ([`session-notes-2026-07-08.md`](session-notes-2026-07-08.md)), which cover the
> supervisor payload-extract / result-surface / Flow-as-Tool fixes.

## What we accomplished

1. **Spiked the lfx variable-store lookup API** (the load-bearing assumption for
   the "Secret Global Variables" choice). Confirmed the encrypted DB read
   (`langflow/services/variable/service.py:180` `get_variable_object`) is keyed on
   `user_id` + name. The subflow components **are** built by lfx per run → they
   carry `user_id`; Python-seam-constructed transports do not. `self.variables(
   name, field)` is lfx's deprecated sync wrapper → `run_until_complete(
   get_variables)` (`lfx/utils/async_helpers.py:22`), safe in both no-loop
   (asyncio.run) and running-loop (new thread) contexts. CREDENTIAL-type values
   return as `pydantic.SecretStr` (unwrap `.get_secret_value()`).

2. **Built `ar_common/vendor_secrets.py`** — `read_secret(component, name,
   default)`: resolution order 1. LangFlow Secret Global Variable (encrypted DB,
   by `user_id` + name, via `component.variables(name, name)`), 2. `os.getenv(
   name)`, 3. `default`. Returns `None` when absent so fail-safe/stub behaviour
   stays. Plus `read_creds(component, names)`.

3. **Built `ar_common/zoho_transport.py` (`RealZoho`)** — pure-Python (no lfx
   import, offline-testable). OAuth refresh-on-401 + POST `/invoices` + DELETE
   `/invoices/{id}`, `organization_id` query param (mirrors
   `ap_tools/zoho_books_ap.py:117-192`). Returns the `StubZohoUpload` dict shape
   (`ok, http_status, code, zoho_id, zoho_ref, duplicate, transient`): 201+body
   `code:0`→`AR_OK`; duplicate `invoice_number` (message "already exists" / Zoho
   codes 1007/36004/36422)→`AR_DUPLICATE` (idempotent); 429/5xx→transient; hard
   4xx→`AR_VALIDATION`/`AR_FORBIDDEN`/`AR_NOT_FOUND`/`AR_UNEXPECTED`; unrecoverable
   OAuth-refresh failure→hard `AR_AUTH`. Maps `InvoiceData`→Zoho body
   (`customer_id`=customer_ref, `date`=issue_date, `currency_code`=currency,
   `line_items[].{name,description,quantity,rate,item_id?}`).

4. **Built `ar_common/foodics_transport.py` (`RealFoodics`)** — pure-Python. OAuth
   2.0 client-id/secret/refresh → 14-day Bearer + `X-Business` header. The 3 AR
   operations (`list_orders`→`orders`, `list_order_items`→`order-products`,
   `list_order_payments`→`payments`), Laravel-style pagination (capped at 100
   pages, §4 fail-safe). Normalizes rows to the canonical column names the flow's
   `_header_map` expects. Returns a JSON §14 envelope string (`{status:ok,
   code:AR_OK, data:{rows:[…]}}`). **Raises transiently** (`_TransientFoodicsError`
   with int `.code`, or `requests` Connection/Timeout) so the §10 loop owns retry;
   hard 4xx → error envelope string. `_make_foodics_fetcher()` matches the
   `tool.operation`/`tool.entity_id`/`tool.fetch_foodics_data()` interface the
   retry loop calls.

5. **Wired both transports into the subflow components' `run()` (lazy, gated on
   creds).** Zoho: `set_transport(RealZoho(creds))` when all four core creds
   present, else keep `StubZohoUpload` (no reset → preserves self-test stubs).
   Foodics: new `set_foodics_creds(creds)` module-global seam set by
   `FoodicsProcessingFlowComponent.run`; `_make_foodics_fetcher()` returns
   `RealFoodics(creds)` when configured else `None` (drops the broken
   `from components.ar_tools.foodics_ar import FoodicsARTool` cross-bundle import
   that was never on `sys.path` → always `None` → `AR_NOT_IMPLEMENTED`). `run()`
   resets `set_foodics_creds(None)` on the no-creds path so a prior run's creds
   never leak in the long-running process.

6. **Deployed (no UUID churn):** re-embedded both edited `.py` byte-identical into
   `ar_issue_invoice.json` + `ar_foodics_processing.json`
   (`nodes[].data.node.template.code.value`, `json.dump(indent=2,
   ensure_ascii=False)`) → in-place `PATCH /api/v1/flows/<UUID>` (live UUIDs
   unchanged → no adapter repoint): `ar_issue_invoice`=b5b49e24…,
   `ar_foodics_processing`=87d38266… → `docker restart aiplatform-langflow-1`
   (the new imported modules `vendor_secrets.py`/`zoho_transport.py`/
   `foodics_transport.py` are cached in `sys.modules`). DB-embedded code verified
   (markers + lengths match disk: 60216 / 96663; persisted across restart).

7. **Verified.** `make test` green (22 suites / 1776 checks) — stub/fail-safe
   paths unchanged. Egress OK (container TCP:443 to `accounts.zoho.com`,
   `www.zohoapis.com`, `api.foodics.com`, `console-sandbox.foodics.com`). SSRF
   confirmed irrelevant (raw `requests` bypass `validate_url_for_ssrf`;
   `LANGFLOW_SSRF_ALLOWED_HOSTS` is a bypass-list not a restrict-to-only gate).
   No-creds regression sweep via the real supervisor REST path: `ar_foodics`
   routes → §19 `pending_approval` (clean); `ar_calculation` → `AR_OK`
   (supervisor + RunFlow path healthy post-restart; first attempt returned
   `AR_UNEXPECTED` only because my test payload used `invoices` instead of the
   `CalculationInputs` `facts`/`parameters` contract — not a deploy regression).

8. **Documented + memory + committed + pushed.** Rewrote
   `cosmic-ar/docs/environment.md` (the credential setup guide — answers the
   user's "how to set up the API keys": per-vendor obtain steps, LangFlow UI
   creation steps, verify commands; corrected the obsolete `FOODICS_API_TOKEN`
   + the SSRF guidance). Closed `V1-STUB` for Zoho + Foodics in
   `cosmic-ar/docs/contracts.md`; updated per-flow docs + `ar_common`/`ar_tools`
   READMEs. New memory `ar-vendor-transports-live.md` + MEMORY.md index line.
   Committed `ec42d07` → pushed `origin/main` FF (`b4c18d7..ec42d07`).

## Key decisions made

- **D1 — Credentials by name from LangFlow Secret Global Variables** (not `.env`,
  not flow inputs). User-confirmed via AskUserQuestion. Single source of truth
  shared with the `ap_tools` components (which read the same names via
  `SecretStrInput(load_from_db=True)`). No `.env` duplication; no flow-JSON input
  surgery.
- **D2 — No `SecretStrInput` added to the subflow components.** The subflow
  component (built by lfx → carries `user_id`) resolves secrets itself via
  `vendor_secrets` and threads plaintext to a pure-Python transport. This avoids
  flow-JSON template surgery, `requiresCredentials` flips, and UUID-churning
  re-imports. `ar_common` stays `requiresCredentials: false`.
- **D3 — Transports are pure-Python in `ar_common`** (importable by the subflow
  modules; no cross-bundle `sys.path` issue; offline-testable; no lfx build-path
  dependency). The `ar_tools.foodics_ar.FoodicsARTool` scaffold is superseded/
  unused for AR.
- **D4 — Absent creds → fail-safe (stub/files path), not an error.** `read_secret`
  returns `None` → Zoho keeps `StubZohoUpload`, Foodics → `None` fetcher →
  `AR_NOT_IMPLEMENTED`/files. `make test` (offline) stays green; no credential is
  required for the bundle to import or the flows to run.
- **D5 — Foodics `set_foodics_creds(None)` reset on no-creds; Zoho no reset.**
  Foodics resets its module global to `None` on the no-creds path so a prior run's
  creds never leak into a later run in the long-running process. Zoho does NOT
  reset (a reset would clobber the self-test's `ScenarioStub`); the only "leak"
  is same-operator creds, which is benign.
- **D6 — `RealFoodics` raises transiently, does not retry internally.** Mirrors
  `FoodicsAPTool`'s `raise_for_status`-shape — the flow's existing §10 loop
  (`_fetch_foodics_with_retry`) owns backoff. Transient = `requests`
  Connection/Timeout (name-matched) or a custom `_TransientFoodicsError(code)`
  (408/429/5xx); hard 4xx → error-envelope string.
- **D7 — Build order: Zoho + Foodics together** (user-confirmed via AskUserQuestion,
  vs. Zoho-first or Foodics-first). One pass, one deploy, one verification sweep.

## Patterns established

- **P1 — `vendor_secrets.read_secret(component, name)` resolution contract.**
  Order: LangFlow Secret Global Variable (encrypted DB, by `user_id` + name, via
  `component.variables(name, name)`; unwrap `pydantic.SecretStr` via
  `get_secret_value()`) → `os.getenv(name)` (offline / no-`user_id` contexts) →
  `default`. The **subflow component** is the resolver (it's the lfx-built object
  with `user_id`); the **transport** stays pure-Python and receives a creds dict.
  Reusable for any future vendor transport.
- **P2 — SSRF-bypass insight (easily wrong).** The vendor transports call
  `requests` directly, so lfx's `validate_url_for_ssrf` does NOT gate them.
  `LANGFLOW_SSRF_ALLOWED_HOSTS` (`lfx/utils/ssrf_protection.py` `is_host_allowed`)
  is a **bypass-list** — allowlisted private hosts skip the blocked-range check;
  it is NOT a restrict-to-only gate, so public vendor hosts are allowed
  regardless. Do not "fix" the allowlist to require the vendor hosts; verify
  container egress (TCP:443) instead. (The docs previously said the vendor hosts
  "must be listed" — corrected in `environment.md`.)
- **P3 — Pure-Python transport contract (StubZohoUpload dict shape).** A
  transport returns `{ok, http_status, code, zoho_id, zoho_ref, duplicate,
  transient}` (create) / `{ok, http_status, code, transient}` (delete) and
  **does not raise** for ordinary API errors — the flow's §10 loop owns retry
  (`_is_transient` = transport-flagged OR 408/429/5xx; hard 4xx no-retry;
  `AR_OK`/`AR_DUPLICATE` stop immediately). Exhausted transient → `AR_UPSTREAM`.
  Keeps the transport offline-testable and the retry policy in one place.
- **P4 — Lazy transport swap in `run()`, gated on creds.** Resolve creds in the
  component's `run()` (before `graph.invoke`), swap via the existing
  (`set_transport`) or new (`set_foodics_creds`) module-global seam only when
  present. Serial subflow runs (sync `graph.invoke`, one subflow at a time under
  the supervisor) make these module globals safe in practice.
- **P5 — Re-embed + in-place PATCH + restart (subflow deploy, no UUID churn).**
  Edit repo `.py` → re-embed byte-identical into `cosmic-ar/flows/<flow>.json`
  `nodes[].data.node.template.code.value` (`json.dump(indent=2,
  ensure_ascii=False)`) → in-place `PATCH /api/v1/flows/<UUID>` (UUID unchanged →
  no adapter repoint) → `docker restart aiplatform-langflow-1` (imported modules
  cached in `sys.modules`). Same mechanism as the supervisor deploy; the restart
  is for the imported transport modules, not the embedded code (which lfx
  recompiles per run).
- **P6 — Live REST run is ground truth, not in-process** (continued from the V1-*
  session). `docker exec aiplatform-langflow-1 curl … /api/v1/run/<UUID>` exercises
  the real `_finalize_envelope`; in-process `tool.ainvoke` can mask defects.

## Next steps

- **N1 — Operator creates the Zoho + Foodics Secret Global Variables** in the
  LangFlow UI (setup guide: `cosmic-ar/docs/environment.md`). Then run the live
  real-vendor verification through the supervisor REST path: `ar_issue_invoice`
  with a valid `approval_ref` → `AR_OK` + real `zoho_id` (idempotent re-run →
  `duplicate=true`); `ar_foodics_processing` (post-approval, `source_mode=api`) →
  `AR_OK` + real sandbox rows.
- **N2 — Verify Foodics sandbox endpoints + token URL against apidocs.foodics.com.**
  The transport's defaults (`api.foodics.com/v2/`, `api.foodics.com/oauth/token`,
  resources `orders`/`order-products`/`payments`) are best-guess v2 conventions;
  the sandbox host + token URL are operator-configurable but need confirming.
- **N3 — Verify Zoho invoice idempotency live.** Confirm the duplicate-
  `invoice_number` detection (message "already exists" / codes 1007/36004/36422)
  and whether Zoho offers an idempotency header for `POST /invoices` (none assumed;
  de-dup is by `invoice_number` uniqueness + duplicate detection).
- **N4 — Infrasys: start the Shiji partner-endorsement email** (developer.hero-
  cloud.com; hk-infrasys-api-enquiry.list@shijigroup.com). Long-lead; no Infrasys
  flow/transport exists yet.
- **N5 — Remaining build-phase caveats (unchanged).** PDF/Excel render
  (`V1-RENDER`), Postgres/Langfuse audit persistence (`V1-NOPERSIST`), full §14
  `data.execution_summary` (`V1-ENVELOPE-META`), LangGraph §19 `Command(resume=)`
  live approval resume, classifier robustness, supervisor self-test harness.

## Blockers

- **None blocking the deploy.** The transports are live in the running stack;
  absent creds keeps the fail-safe path; `make test` green; egress + no-creds
  regression verified.
- **Live real-vendor test is gated on the operator** creating the Secret Global
  Variables (the user's "how to set up the API keys" question — answered in
  `environment.md`). Not a code blocker.
- **Infrasys is long-lead** (Shiji partner endorsement) — by design out of scope
  this pass; start the email in parallel.

## Commits this session

- `ec42d07` — `feat(cosmic-ar): real Zoho Books + Foodics sandbox transports`
  (vendor_secrets + RealZoho + RealFoodics + wiring + re-embedded flow JSONs +
  docs/READMEs). Pushed to `origin/main` (fast-forward, `b4c18d7..ec42d07`).
  `graphify update .` run after.