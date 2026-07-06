# Reusable LangFlow Components (`cosmic_common`)

This is the per-component reference for the 15 generic, reusable `lfx`
Components shipped in the **`cosmic_common`** runtime bundle
(`docker/langflow-extensions/cosmic_common/`). Each component is a valid,
importable LangFlow Component (skeleton only — no business logic yet) that emits
the §14 envelope and **never raises** out of its output method.

> **Constitution §15 reuse check (binding).** LangFlow 1.10 ships built-in file
> components (Excel/CSV/PDF). The custom readers here are a **recorded waiver**
> (see [ADR-0002](adr/adr-0002-reusable-component-library.md)): they emit output
> normalized to the project's [contracts](contracts.md) inside the §14 envelope
> (built-ins don't), apply the §16 PII/SSRF rules, and are reusable across AR and
> the future AP extension (§20). Every other component has **no built-in
> equivalent** in LangFlow 1.10, so §15's "check built-ins first" is satisfied
> with no waiver needed.

## Build-phase dependencies (recorded, not installed)

The bundle's `pyproject.toml` declares `dependencies = []` (it relies on the
langflow image venv, mirroring `ar_common`/`ar_tools`). The libs below are **not
confirmed in the image** and must be baked into `docker/langflow/Dockerfile` at
the build phase — same shape as the architecture's
`langgraph-checkpoint-postgres` note. No `Dockerfile` edit is made by the
scaffolding task; this table is the build-phase checklist.

| # | Component | Build-phase lib | In-image? |
|---|-----------|-----------------|-----------|
| 1 | Excel Reader | `openpyxl` | no — bake at build phase |
| 2 | CSV Reader | stdlib `csv` | yes |
| 3 | PDF Reader | `pdfplumber` | no — bake at build phase |
| 4 | Document Classifier | LangChain (in-image) | yes |
| 5 | Excel Normalizer | stdlib `decimal`/`datetime` | yes |
| 6 | Business Rule Engine | stdlib | yes |
| 7 | Validation Engine | `jsonschema` | no — bake at build phase |
| 8 | Calculation Engine | stdlib `decimal` | yes |
| 9 | Invoice Builder | stdlib `decimal` | yes |
| 10 | Zoho Connector | `requests` (in-image) | yes |
| 11 | Audit Logger | `sqlalchemy` (in-image) | yes |
| 12 | Notification | `requests`/smtplib (in-image) | yes |
| 13 | Checkpoint Manager | `langgraph-checkpoint-postgres` | no — bake at build phase |
| 14 | State Manager | stdlib | yes |
| 15 | Configuration Loader | stdlib | yes |

Credentials referenced (Secret Global Variables only, §16 — never hard-coded,
never in flow JSON): `ZOHO_*` (Zoho Connector), `SMTP_*` / channel creds
(Notification). Wiring is build-phase.

The bundle README
(`docker/langflow-extensions/cosmic_common/README.md`) has the validate command;
post-deploy: `docker exec langflow python -m lfx extension validate
/app/extensions/cosmic_common`.

---

Each section below has the nine facets: **Purpose** · **Inputs** · **Outputs** ·
**Configuration** · **Python Template** · **Dependencies** · **Error Handling** ·
**Logging** · **Future Reuse Guidance**.

## 1. Excel Reader — `ExcelReaderComponent`

- **Purpose.** Read a spreadsheet into typed rows normalized to the
  [DocumentManifest](contracts.md) shape. Custom reader per
  ADR-0002 (emits contract-conformant output + §16 rules). Constitution §8.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `file_path` | MessageTextInput | yes | tool_mode |
  | `sheet_name` | MessageTextInput | no | defaults to first sheet |
  | `range` | MessageTextInput | no | e.g. `A1:D100` |
  | `has_header` | BoolInput | no | default true |
  | `max_rows` | IntInput | no | 0 = unlimited |

- **Outputs.** `read` → envelope `data` = `{rows: [...]}`.
- **Configuration.** Sheet name, cell range, header flag, row cap.
- **Python Template.**

  ```python
  class ExcelReaderComponent(Component):
      name = "ExcelReaderComponent"
      display_name = "Excel Reader"
      icon = "Sheet"
      inputs = [MessageTextInput(name="file_path", required=True, tool_mode=True),
                MessageTextInput(name="sheet_name", tool_mode=True),
                MessageTextInput(name="range", tool_mode=True),
                BoolInput(name="has_header", value=True),
                IntInput(name="max_rows", value=0)]
      outputs = [Output(display_name="Rows", method="read")]
      def read(self) -> Message: ...  # emits §14 envelope; never raises
  ```

- **Dependencies.** `openpyxl` — bake at build phase (not in image).
- **Error Handling.** Missing/unreadable file → `code=AR_IO` (§9); malformed
  range → `AR_VALIDATION`. Never raises.
- **Logging.** `self.log` event `excel.read`, outcome `ok/failed`, `trace_id`;
  no file contents inlined (§12/§16).
- **Future Reuse Guidance.** Reused verbatim by the AP extension (§20) for
  vendor-bill ingestion; the AR read subflow calls it directly.

## 2. CSV Reader — `CSVReaderComponent`

- **Purpose.** Read a CSV into typed rows → [DocumentManifest](contracts.md).
  Constitution §8.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `file_path` | MessageTextInput | yes | tool_mode |
  | `delimiter` | MessageTextInput | no | default `,` |
  | `has_header` | BoolInput | no | default true |
  | `max_rows` | IntInput | no | 0 = unlimited |

- **Outputs.** `read` → envelope `data` = `{rows: [...]}`.
- **Configuration.** Delimiter, header flag, row cap.
- **Python Template.**

  ```python
  class CSVReaderComponent(Component):
      name = "CSVReaderComponent"
      display_name = "CSV Reader"
      icon = "FileText"
      inputs = [MessageTextInput(name="file_path", required=True, tool_mode=True),
                MessageTextInput(name="delimiter", value=","),
                BoolInput(name="has_header", value=True),
                IntInput(name="max_rows", value=0)]
      outputs = [Output(display_name="Rows", method="read")]
  ```

- **Dependencies.** stdlib `csv` (in image).
- **Error Handling.** Missing file → `AR_IO`; bad delimiter → `AR_VALIDATION`.
  Never raises.
- **Logging.** event `csv.read`, outcome + `trace_id`; no contents inlined.
- **Future Reuse Guidance.** AP extension reuses for bank-statement CSV ingest.

## 3. PDF Reader — `PDFReaderComponent`

- **Purpose.** Extract text (and optionally tables) from a PDF →
  [DocumentManifest](contracts.md). Custom reader per ADR-0002.
  Constitution §8.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `file_path` | MessageTextInput | yes | tool_mode |
  | `pages` | MessageTextInput | no | e.g. `1-3` |
  | `extract_tables` | BoolInput | no | default false |

- **Outputs.** `read` → envelope `data` = `{text, tables?}`.
- **Configuration.** Page range, table-extraction flag.
- **Python Template.**

  ```python
  class PDFReaderComponent(Component):
      name = "PDFReaderComponent"
      display_name = "PDF Reader"
      icon = "FileType"
      inputs = [MessageTextInput(name="file_path", required=True, tool_mode=True),
                MessageTextInput(name="pages", tool_mode=True),
                BoolInput(name="extract_tables", value=False)]
      outputs = [Output(display_name="Text/Tables", method="read")]
  ```

- **Dependencies.** `pdfplumber` — bake at build phase (not in image).
- **Error Handling.** Encrypted/missing PDF → `AR_IO`. Never raises.
- **Logging.** event `pdf.read`, outcome + `trace_id`; no extracted text inlined.
- **Future Reuse Guidance.** AP extension reuses for vendor-bill PDFs.

## 4. Document Classifier — `DocumentClassifierComponent`

- **Purpose.** Label a document (invoice / receipt / credit_note / statement /
  unknown) with a confidence score. Low confidence → `AR_UNCERTAIN` (§4 fail-safe;
  caller escalates rather than guesses). Constitution §4.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `document_ref` | MessageTextInput | yes | tool_mode |
  | `content_ref` | MessageTextInput | yes | reader output handle |
  | `candidate_types` | MultilineInput | no | one per line |
  | `rules_ref` | MessageTextInput | no | override rule set |
  | `min_confidence` | FloatInput | no | default 0.8 |

- **Outputs.** `classify` → `{document, doc_type, confidence}`; below threshold
  sets `code=AR_UNCERTAIN`.
- **Configuration.** Candidate types, min-confidence threshold, optional rules.
- **Python Template.**

  ```python
  class DocumentClassifierComponent(Component):
      name = "DocumentClassifierComponent"
      display_name = "Document Classifier"
      icon = "Tag"
      inputs = [MessageTextInput(name="document_ref", required=True, tool_mode=True),
                MessageTextInput(name="content_ref", required=True, tool_mode=True),
                MultilineInput(name="candidate_types", tool_mode=True),
                MessageTextInput(name="rules_ref", tool_mode=True),
                FloatInput(name="min_confidence", value=0.8)]
      outputs = [Output(display_name="Classification", method="classify")]
  ```

- **Dependencies.** LangChain (in image) at build phase for LLM-driven
  classification layered over rule-based.
- **Error Handling.** Below `min_confidence` → `AR_UNCERTAIN` (§4); classifier
  error → `AR_PROCESSING`. Never raises.
- **Logging.** event `document.classify`, `doc_type`, `confidence` (not content),
  outcome + `trace_id`.
- **Future Reuse Guidance.** Reused by AP extension to route vendor-bill PDFs.

## 5. Excel Normalizer — `ExcelNormalizerComponent`

- **Purpose.** Coerce messy spreadsheet rows into typed rows: header mapping,
  ISO-8601 dates, **2-decimal-string amounts** (project-wide amount rule). Run
  right after a reader. Constitution §8.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `raw_rows` | MultilineInput | yes | JSON array, tool_mode |
  | `header_map` | MultilineInput | no | JSON `{src: canonical}` |
  | `amount_columns` | MultilineInput | no | one column per line → 2dp strings |
  | `date_columns` | MultilineInput | no | one column per line → ISO-8601 |
  | `currency` | MessageTextInput | no | default `SAR` |

- **Outputs.** `normalize` → envelope `data` = `{rows: [...]}` (amounts as
  `^-?\d+\.\d{2}$`).
- **Configuration.** Header map, amount/date columns, currency stamp.
- **Python Template.**

  ```python
  class ExcelNormalizerComponent(Component):
      name = "ExcelNormalizerComponent"
      display_name = "Excel Normalizer"
      icon = "Eraser"
      inputs = [MultilineInput(name="raw_rows", required=True, tool_mode=True),
                MultilineInput(name="header_map", tool_mode=True),
                MultilineInput(name="amount_columns", tool_mode=True),
                MultilineInput(name="date_columns", tool_mode=True),
                MessageTextInput(name="currency", value="SAR")]
      outputs = [Output(display_name="Normalized Rows", method="normalize")]
  ```

- **Dependencies.** stdlib `decimal`/`datetime` (in image).
- **Error Handling.** Uncoercible amount/date → `AR_VALIDATION` with the row
  index. Never raises.
- **Logging.** event `excel.normalize`, row count, outcome + `trace_id`; no
  cell values inlined.
- **Future Reuse Guidance.** AP extension reuses for vendor-bill normalization.

## 6. Business Rule Engine — `BusinessRuleEngineComponent`

- **Purpose.** Evaluate a declarative rule set against a payload; emit per-rule
  pass/fail. Enforce AR business rules (e.g. "no match above the auto ceiling
  without approval") before any financial action. Constitution §9.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `rules` | MultilineInput | yes | JSON array of rules, tool_mode |
  | `payload` | MultilineInput | yes | JSON object, tool_mode |
  | `strict` | BoolInput | no | failing rule ⇒ overall error |

- **Outputs.** `evaluate` → `{results: [{rule_id, passed, message}]}`.
- **Configuration.** Rule set, strict mode.
- **Python Template.**

  ```python
  class BusinessRuleEngineComponent(Component):
      name = "BusinessRuleEngineComponent"
      display_name = "Business Rule Engine"
      icon = "ListChecks"
      inputs = [MultilineInput(name="rules", required=True, tool_mode=True),
                MultilineInput(name="payload", required=True, tool_mode=True),
                BoolInput(name="strict", value=False)]
      outputs = [Output(display_name="Rule Results", method="evaluate")]
  ```

- **Dependencies.** stdlib (in image).
- **Error Handling.** Malformed rules → `AR_VALIDATION` (§9); strict failure →
  `AR_RULE_FAILED`. Never raises.
- **Logging.** event `rules.evaluate`, rule count, outcome + `trace_id`; rule
  ids only, not payload values.
- **Future Reuse Guidance.** AP extension reuses for 3-way-match rules.

## 7. Validation Engine — `ValidationEngineComponent`

- **Purpose.** Validate a JSON payload against one of the 14
  [contracts](contracts.md) and emit a [ValidationResult](contracts.md).
  Call before posting to Zoho or persisting state. Constitution §8/§9.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `contract_name` | DropdownInput | yes | one of the 14 contracts |
  | `payload` | MultilineInput | yes | JSON, tool_mode |
  | `trace_id` | MessageTextInput | no | propagated into result |

- **Outputs.** `validate` → [ValidationResult](contracts.md).
- **Configuration.** Contract selection (reuses `cosmic-ar/contracts/schemas/`;
  no schema duplicated here).
- **Python Template.**

  ```python
  class ValidationEngineComponent(Component):
      name = "ValidationEngineComponent"
      display_name = "Validation Engine"
      icon = "ShieldCheck"
      inputs = [DropdownInput(name="contract_name", options=[...14...], value="InvoiceData", tool_mode=True),
                MultilineInput(name="payload", required=True, tool_mode=True),
                MessageTextInput(name="trace_id", tool_mode=True)]
      outputs = [Output(display_name="Validation Result", method="validate")]
  ```

- **Dependencies.** `jsonschema` — bake at build phase (not in image).
- **Error Handling.** Invalid payload → `AR_VALIDATION` with the JSON-Schema
  error path; missing schema → `AR_CONFIG`. Never raises.
- **Logging.** event `validate`, `contract_name`, `valid`, outcome + `trace_id`;
  no payload inlined.
- **Future Reuse Guidance.** Universal — every subflow validates before write;
  AP extension reuses the same 14-contract dropdown.

## 8. Calculation Engine — `CalculationEngineComponent`

- **Purpose.** Run a named financial calculation (match / reconcile / aging /
  rounding) and emit a [CalculationResult](contracts.md).
  Constitution §8.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `calculation_type` | DropdownInput | yes | match\|reconcile\|aging\|rounding |
  | `inputs` | MultilineInput | yes | JSON inputs, tool_mode |
  | `inputs_ref` | MessageTextInput | no | echoed in result |
  | `currency` | MessageTextInput | no | default `SAR` |

- **Outputs.** `calculate` → [CalculationResult](contracts.md).
- **Configuration.** Calculation type, currency.
- **Python Template.**

  ```python
  class CalculationEngineComponent(Component):
      name = "CalculationEngineComponent"
      display_name = "Calculation Engine"
      icon = "Calculator"
      inputs = [DropdownInput(name="calculation_type", options=["match","reconcile","aging","rounding"], value="match", tool_mode=True),
                MultilineInput(name="inputs", required=True, tool_mode=True),
                MessageTextInput(name="inputs_ref", tool_mode=True),
                MessageTextInput(name="currency", value="SAR")]
      outputs = [Output(display_name="Calculation Result", method="calculate")]
  ```

- **Dependencies.** stdlib `decimal` (in image) — 2-decimal-string math.
- **Error Handling.** Mismatched inputs → `AR_VALIDATION`; numeric overflow →
  `AR_CALCULATION`. Never raises.
- **Logging.** event `calc.<type>`, totals (not line items), outcome +
  `trace_id`.
- **Future Reuse Guidance.** AP extension reuses `reconcile`/`aging` for
  vendor-bill aging; AR uses `match` for invoice↔receipt matching.

## 9. Invoice Builder — `InvoiceBuilderComponent`

- **Purpose.** Assemble a contract-conformant [InvoiceData](contracts.md)
  from customer + line items, computing totals with 2-decimal amounts.
  Constitution §8.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `customer_ref` | MessageTextInput | yes | id only, tool_mode |
  | `line_items` | MultilineInput | yes | JSON array, tool_mode |
  | `currency` | MessageTextInput | no | default `SAR` |
  | `issue_date` | MessageTextInput | no | ISO-8601 date |
  | `due_date` | MessageTextInput | no | ISO-8601 date |
  | `tax_rate` | MessageTextInput | no | decimal fraction, default `0.15` |
  | `discounts` | MultilineInput | no | JSON array |

- **Outputs.** `build` → [InvoiceData](contracts.md).
- **Configuration.** Tax rate, currency, dates, discounts.
- **Python Template.**

  ```python
  class InvoiceBuilderComponent(Component):
      name = "InvoiceBuilderComponent"
      display_name = "Invoice Builder"
      icon = "ReceiptText"
      inputs = [MessageTextInput(name="customer_ref", required=True, tool_mode=True),
                MultilineInput(name="line_items", required=True, tool_mode=True),
                MessageTextInput(name="currency", value="SAR"),
                MessageTextInput(name="issue_date", tool_mode=True),
                MessageTextInput(name="due_date", tool_mode=True),
                MessageTextInput(name="tax_rate", value="0.15"),
                MultilineInput(name="discounts", tool_mode=True)]
      outputs = [Output(display_name="Invoice Data", method="build")]
  ```

- **Dependencies.** stdlib `decimal` (in image).
- **Error Handling.** Bad line item → `AR_VALIDATION`; total mismatch →
  `AR_CALCULATION`. Never raises.
- **Logging.** event `invoice.build`, line-item count, totals (not customer
  PII), outcome + `trace_id`.
- **Future Reuse Guidance.** AR posts invoices to Zoho; AP extension will compose
  it in reverse for credit-note generation.

## 10. Zoho Connector — `ZohoConnectorComponent`

- **Purpose.** Generic CRUD base for the Zoho Finance Suite API. Credentials
  ONLY from Secret Global Variables via `SecretStrInput(..., load_from_db=True)`
  (§16). Build phase implements §10 retry (3 attempts, exp backoff, idempotency
  keys) + §16 SSRF guards + OAuth refresh-on-401. Constitution §10/§16.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `zoho_client_id` | SecretStrInput | yes | Secret Global Var, load_from_db |
  | `zoho_client_secret` | SecretStrInput | yes | Secret Global Var, load_from_db |
  | `zoho_refresh_token` | SecretStrInput | yes | Secret Global Var, load_from_db |
  | `zoho_access_token` | SecretStrInput | no | cached, refresh on 401 |
  | `organization_id` | MessageTextInput | yes | tool_mode |
  | `api_url` | MessageTextInput | yes | tool_mode |
  | `accounts_url` | MessageTextInput | yes | OAuth refresh, tool_mode |
  | `resource` | DropdownInput | yes | invoices\|creditnotes\|customerpayments\|… |
  | `method` | DropdownInput | yes | GET\|POST\|PUT |
  | `entity_id` | MessageTextInput | no | tool_mode |
  | `query_params` | MessageTextInput | no | JSON, tool_mode |
  | `body` | MultilineInput | no | JSON, tool_mode |

- **Outputs.** `call` → [ZohoUploadResult](contracts.md) for
  writes, or a fetch envelope for reads.
- **Configuration.** API/accounts URLs, organization id, resource, method.
- **Python Template.**

  ```python
  class ZohoConnectorComponent(Component):
      name = "ZohoConnectorComponent"
      display_name = "Zoho Connector"
      icon = "Plug"
      inputs = [SecretStrInput(name="zoho_client_id", required=True, load_from_db=True),
                SecretStrInput(name="zoho_client_secret", required=True, load_from_db=True),
                SecretStrInput(name="zoho_refresh_token", required=True, load_from_db=True),
                SecretStrInput(name="zoho_access_token", load_from_db=True),
                MessageTextInput(name="organization_id", required=True, tool_mode=True),
                MessageTextInput(name="api_url", required=True, tool_mode=True),
                MessageTextInput(name="accounts_url", required=True, tool_mode=True),
                DropdownInput(name="resource", options=[...], value="invoices", tool_mode=True),
                DropdownInput(name="method", options=["GET","POST","PUT"], value="GET", tool_mode=True),
                MessageTextInput(name="entity_id", tool_mode=True),
                MessageTextInput(name="query_params", tool_mode=True),
                MultilineInput(name="body", tool_mode=True)]
      outputs = [Output(display_name="Zoho Result", method="call")]
  ```

- **Dependencies.** `requests` (in image).
- **Error Handling.** Transient 5xx/429 → §10 retry with idempotency key; 401 →
  refresh token then retry once; 4xx (non-401) → `AR_ZOHO_API`; SSRF-blocked
  host → `AR_SSRF` (§16). Never raises.
- **Logging.** event `zoho.<method>.<resource>`, `http_status`, `zoho_request_id`,
  outcome + `trace_id`; **no** tokens/bodies inlined (§16).
- **Future Reuse Guidance.** `ar_tools.ZohoBooksARTool` composes this for AR
  (invoices, credit notes, customer payments). AP extension composes it for
  vendor bills and bill payments.

## 11. Audit Logger — `AuditLoggerComponent`

- **Purpose.** Append-only [AuditRecord](contracts.md) writer.
  `append_only` is a constant true (§13). `actor` = Keycloak `sub` (§13);
  `before`/`after` reference state by id, never PII (§12/§16). Constitution §13.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `actor` | MessageTextInput | yes | Keycloak sub, tool_mode |
  | `action` | MessageTextInput | yes | e.g. `ar.invoice.post` |
  | `before` | MultilineInput | no | state ref by id |
  | `after` | MultilineInput | no | state ref by id |
  | `approval_ref` | MessageTextInput | no | §19, if authorized |
  | `idempotency_key` | MessageTextInput | no | §10 |
  | `trace_id` | MessageTextInput | no | §12 |
  | `tenant` | MessageTextInput | no | multi-tenant |
  | `source_system` | DropdownInput | no | cosmic-ar-agent\|librechat\|langflow\|manual\|scheduled |

- **Outputs.** `write` → [AuditRecord](contracts.md).
- **Configuration.** `append_only` const true (cannot be overridden from flow
  JSON); source-system enum.
- **Python Template.**

  ```python
  class AuditLoggerComponent(Component):
      name = "AuditLoggerComponent"
      display_name = "Audit Logger"
      icon = "FileClock"
      append_only = True  # §13: immutable
      inputs = [MessageTextInput(name="actor", required=True, tool_mode=True),
                MessageTextInput(name="action", required=True, tool_mode=True),
                MultilineInput(name="before", tool_mode=True),
                MultilineInput(name="after", tool_mode=True),
                MessageTextInput(name="approval_ref", tool_mode=True),
                MessageTextInput(name="idempotency_key", tool_mode=True),
                MessageTextInput(name="trace_id", tool_mode=True),
                MessageTextInput(name="tenant", tool_mode=True),
                DropdownInput(name="source_system", options=[...], value="cosmic-ar-agent", tool_mode=True)]
      outputs = [Output(display_name="Audit Record", method="write")]
  ```

- **Dependencies.** `sqlalchemy` (in image) → Postgres `audit` table at build
  phase.
- **Error Handling.** DB write failure → `AR_PERSISTENCE`; **never** silently
  drops an audit record (retries per §10, then surfaces the failure). Never
  raises.
- **Logging.** event `audit.write`, `action`, `actor` (sub only), outcome +
  `trace_id`; no `before`/`after` content inlined.
- **Future Reuse Guidance.** `ar_common.AuditRecordComponent` composes this with
  AR-specific action namespaces; AP extension composes it identically.

## 12. Notification — `NotificationComponent`

- **Purpose.** Send on email / sms / chat / in-app from a template + referenced
  content. Recipients (`recipient_ref`) and body (`body_ref`) are referenced,
  not inlined (§12/§16 PII rule). Used for dunning, approval requests, run
  summaries. Constitution §12/§16.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `channel` | DropdownInput | yes | email\|sms\|chat\|in_app |
  | `recipient_ref` | MessageTextInput | yes | id only, tool_mode |
  | `template` | MessageTextInput | yes | named template (§17) |
  | `approval_ref` | MessageTextInput | no | §19 |
  | `dunning_level` | IntInput | no | 0..3 |
  | `subject_ref` | MessageTextInput | no | rendered subject by id |
  | `body_ref` | MessageTextInput | no | rendered body by id |
  | `channel_secret` | SecretStrInput | yes | SMTP_*/channel cred, load_from_db |

- **Outputs.** `send` → [Notification](contracts.md).
- **Configuration.** Channel, template, dunning level.
- **Python Template.**

  ```python
  class NotificationComponent(Component):
      name = "NotificationComponent"
      display_name = "Notification"
      icon = "Bell"
      inputs = [DropdownInput(name="channel", options=["email","sms","chat","in_app"], value="email", tool_mode=True),
                MessageTextInput(name="recipient_ref", required=True, tool_mode=True),
                MessageTextInput(name="template", required=True, tool_mode=True),
                MessageTextInput(name="approval_ref", tool_mode=True),
                IntInput(name="dunning_level", value=0),
                MessageTextInput(name="subject_ref", tool_mode=True),
                MessageTextInput(name="body_ref", tool_mode=True),
                SecretStrInput(name="channel_secret", required=True, load_from_db=True)]
      outputs = [Output(display_name="Notification", method="send")]
  ```

- **Dependencies.** `requests`/smtplib (in image) per channel.
- **Error Handling.** Channel down → §10 retry then `AR_NOTIFICATION`; bad
  template → `AR_CONFIG`. Never raises.
- **Logging.** event `notify.<channel>`, `template`, `dunning_level`,
  `delivered`, outcome + `trace_id`; **no** recipient address or body content
  inlined.
- **Future Reuse Guidance.** AR dunning + approval flows; AP extension reuses
  for vendor-bill approval notifications.

## 13. Checkpoint Manager — `CheckpointManagerComponent`

- **Purpose.** Save / load / list agent-state checkpoints so a run resumes
  after interruption. Per §11, checkpoints are **self-sufficient** — they do not
  rely on Langfuse spans (tracing currently OFF,
  `LANGFLOW_DEACTIVATE_TRACING=true`). Constitution §11.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `operation` | DropdownInput | yes | save\|load\|list |
  | `checkpoint_id` | MessageTextInput | no | for load |
  | `agent_state` | MultilineInput | no | full AgentState JSON for save |
  | `trace_id` | MessageTextInput | no | §12 |

- **Outputs.** `manage` → checkpoint envelope (`{operation, checkpoint_id?}`).
- **Configuration.** Backend selection (Postgres primary, MemorySaver fallback)
  is build-phase wiring, not a flow input.
- **Python Template.**

  ```python
  class CheckpointManagerComponent(Component):
      name = "CheckpointManagerComponent"
      display_name = "Checkpoint Manager"
      icon = "DatabaseBackup"
      inputs = [DropdownInput(name="operation", options=["save","load","list"], value="save", tool_mode=True),
                MessageTextInput(name="checkpoint_id", tool_mode=True),
                MultilineInput(name="agent_state", tool_mode=True),
                MessageTextInput(name="trace_id", tool_mode=True)]
      outputs = [Output(display_name="Checkpoint Result", method="manage")]
  ```

- **Dependencies.** `langgraph-checkpoint-postgres` — bake at build phase (not in
  image); MemorySaver is the documented fallback.
- **Error Handling.** DB unavailable → fall back to MemorySaver + warn; corrupt
  checkpoint → `AR_CHECKPOINT`. Never raises.
- **Logging.** event `checkpoint.<operation>`, `checkpoint_id`, outcome +
  `trace_id`; the persisted state is opaque in logs (§12).
- **Future Reuse Guidance.** `ar_common.CheckpointComponent` composes this for the
  AR supervisor; AP extension composes it for its own supervisor.

## 14. State Manager — `StateManagerComponent`

- **Purpose.** Get / set / merge / snapshot the typed `AgentState` immutably
  (§8 — `set`/`merge` return new snapshots, no in-place mutation). Generic base
  carrying no AR-specific fields; the AR `AgentState` lives in `ar_common`.
  Constitution §8.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `operation` | DropdownInput | yes | get\|set\|merge\|snapshot |
  | `state_ref` | MessageTextInput | yes | current state id |
  | `fragment` | MultilineInput | no | JSON to set/merge |
  | `trace_id` | MessageTextInput | no | §12 |

- **Outputs.** `manage` → state envelope (AgentState-shaped).
- **Configuration.** Operation enum; immutability is enforced in the method, not
  a knob.
- **Python Template.**

  ```python
  class StateManagerComponent(Component):
      name = "StateManagerComponent"
      display_name = "State Manager"
      icon = "Workflow"
      inputs = [DropdownInput(name="operation", options=["get","set","merge","snapshot"], value="get", tool_mode=True),
                MessageTextInput(name="state_ref", required=True, tool_mode=True),
                MultilineInput(name="fragment", tool_mode=True),
                MessageTextInput(name="trace_id", tool_mode=True)]
      outputs = [Output(display_name="State Result", method="manage")]
  ```

- **Dependencies.** stdlib (in image).
- **Error Handling.** Unknown `state_ref` → `AR_STATE`; bad fragment →
  `AR_VALIDATION`. Never raises.
- **Logging.** event `state.<operation>`, outcome + `trace_id`; no state values
  inlined (§12).
- **Future Reuse Guidance.** The supervisor and every subflow compose this; AP
  extension builds its own `AgentState` on top.

## 15. Configuration Loader — `ConfigurationLoaderComponent`

- **Purpose.** Load non-secret run tunables (thresholds, dunning cadence,
  approval ceilings, feature flags) from LangFlow Global Variables / per-flow
  config (§17). Secrets are **never** read here (§16). Constitution §17.
- **Inputs.**

  | name | type | required | note |
  |------|------|----------|------|
  | `config_ref` | DropdownInput | yes | ar.thresholds\|ar.dunning\|ar.approval\|ar.matching\|ar.tenants\|ar.feature_flags |
  | `keys` | MultilineInput | no | one key per line filter |
  | `tenant` | MessageTextInput | no | tenant-scoped override |

- **Outputs.** `load` → config envelope (`{config_ref, values}`).
- **Configuration.** Named config set, optional key filter, tenant scope.
- **Python Template.**

  ```python
  class ConfigurationLoaderComponent(Component):
      name = "ConfigurationLoaderComponent"
      display_name = "Configuration Loader"
      icon = "Cog"
      inputs = [DropdownInput(name="config_ref", options=["ar.thresholds","ar.dunning","ar.approval","ar.matching","ar.tenants","ar.feature_flags"], value="ar.thresholds", tool_mode=True),
                MultilineInput(name="keys", tool_mode=True),
                MessageTextInput(name="tenant", tool_mode=True)]
      outputs = [Output(display_name="Config", method="load")]
  ```

- **Dependencies.** stdlib (in image) — reads env / Global Variables.
- **Error Handling.** Missing config → `AR_CONFIG`; never falls back to a
  hard-coded default silently (surfaces the gap). Never raises.
- **Logging.** event `config.load`, `config_ref`, key count, outcome + `trace_id`;
  values logged only if non-secret.
- **Future Reuse Guidance.** Universal — AR and AP both resolve tunables through
  it; the dunning cadence and approval ceilings live here as named configs.